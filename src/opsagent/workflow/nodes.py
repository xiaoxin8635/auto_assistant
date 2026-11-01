"""工作流节点实现：triage / collect / analyze / execute / validate（DESIGN §5.5.2 P1）。

P1 简化（对齐 DESIGN 分期）：
- 无 RAG 佐证、无假设/验证循环：analyze 单轮直出根因与修复方案
- collect 只暴露低危工具（registry 中 risk=="low"）；check_recovery 由工作流持有，不进 LLM 工具集
- summaries 为静态摘要（分层记忆的模型生成版 P2 引入）

工厂闭包模式：make_xxx(...) 返回节点函数，依赖（LLM/registry/audit）在装配期注入，
节点函数签名保持 LangGraph 标准 (state) -> partial-update。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from opsagent.config import WorkflowConfig
from opsagent.errors import (
    WF_3001_ANALYSIS_OUTPUT_INVALID,
    WF_3002_MAX_ROUNDS_EXCEEDED,
    WF_3003_TOKEN_BUDGET_EXCEEDED,
    OpsAgentError,
)
from opsagent.governance.audit import AuditLogger
from opsagent.infra.llm import LLMAdapter, LLMResponse
from opsagent.tools.base import BaseTool, ToolRegistry, ToolResult
from opsagent.workflow.state import DiagnosisState

# collect 节点内单次进入的最大 function-calling 轮数（超出则带现有证据进入 analyze）
_COLLECT_MAX_TOOL_ROUNDS = 3

_TRIAGE_SYSTEM_PROMPT = (
    "你是运维告警受理员。从原始告警 JSON 中提取结构化字段，只输出一个 JSON 对象："
    '{"host_id": str, "service": str, "severity": str, "summary": str, "metric": str|null, "value": number|null}。'
    "host_id 与 service 必须保留告警原文；无法确定的字段填 null。不要输出 JSON 以外的任何内容。"
)

_COLLECT_SYSTEM_PROMPT = (
    "你是运维诊断员。只处理当前告警，不发散到其他主机或服务。"
    "可调用查询工具（指标/日志/CMDB）收集证据；证据足够定位根因后，停止调用工具并用一句话给出结论。"
    "同一工具同一参数不要重复调用。"
)

_ANALYZE_SYSTEM_PROMPT = (
    "你是运维根因分析专家。基于告警与证据输出唯一 JSON 对象："
    '{"root_cause": str, "confidence": number(0~1), "fix_action": str, "fix_params": object, "reason": str}。'
    "fix_action 必须是可执行修复动作（当前支持 restart_service）；"
    "fix_params 需包含该动作所需全部参数（restart_service 需要 host_id 与 service_name）。只输出 JSON。"
)

_REPORT_SYSTEM_PROMPT = (
    "你是运维报告撰写员。基于根因、执行动作与恢复验证数据，生成不超过 200 字的中文处理报告，"
    "需覆盖：根因、执行了什么动作、恢复验证结果。直接输出文本，不要 JSON。"
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _elapsed_ms(started: float) -> float:
    """计算节点耗时（毫秒，保留 3 位小数）。

    Args:
        started: time.perf_counter 起点值。

    Returns:
        耗时毫秒数。
    """
    return round((time.perf_counter() - started) * 1000, 3)


def _halt(status: str, code: str, message: str) -> dict[str, Any]:
    """构造非 running 终止更新（状态 + 错误体），路由层据此收敛到 END。

    Args:
        status: 终态（closed/escalated/failed）。
        code: 错误码。
        message: 错误描述。

    Returns:
        部分状态更新 dict。
    """
    return {"status": status, "error": {"code": code, "message": message}}


def _parse_json_content(content: str | None) -> dict[str, Any] | None:
    """解析 LLM 输出中的 JSON 对象（容忍 ```json 围栏）。

    Args:
        content: 模型文本输出。

    Returns:
        解析出的 dict；非 dict / 解析失败 / 空输入返回 None。
    """
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _evidence_signature(tool_name: str, params: dict[str, Any]) -> str:
    """计算证据签名（工具名 + 参数规范化 JSON），用于去重。

    Args:
        tool_name: 工具名。
        params: 调用参数。

    Returns:
        签名字符串。
    """
    return f"{tool_name}:{json.dumps(params, sort_keys=True, ensure_ascii=False)}"


def make_triage(llm: LLMAdapter, audit: AuditLogger, cfg: WorkflowConfig) -> Any:
    """构造 triage 节点：fast 档解析原始告警 → 结构化字段；失败转人工 WF-3001。

    Args:
        llm: LLM 适配器。
        audit: 审计器。
        cfg: 工作流配置（预算等）。

    Returns:
        LangGraph 节点函数。
    """

    def triage(state: DiagnosisState) -> dict[str, Any]:
        started = time.perf_counter()
        task_id = state["task_id"]
        raw = state["alert"]["raw"]
        messages = [
            {"role": "system", "content": _TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(raw, ensure_ascii=False)},
        ]
        try:
            resp = llm.chat(messages, tier="fast")
        except OpsAgentError as exc:
            audit.record_llm(
                task_id, "triage", tier="fast", model="", messages=messages,
                error_code=exc.code, elapsed_ms=_elapsed_ms(started),
            )
            audit.record_node(task_id, "triage", input={"alert_id": raw.get("alert_id")}, output={"error": exc.code}, elapsed_ms=_elapsed_ms(started))
            return _halt("escalated", exc.code, exc.message)
        token_used = state["token_used"] + resp.usage.total_tokens
        audit.record_llm(
            task_id, "triage", tier="fast", model=resp.model, messages=messages,
            response_text=resp.content or "", prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens, elapsed_ms=_elapsed_ms(started),
        )
        parsed = _parse_json_content(resp.content)
        if parsed is None or not isinstance(parsed.get("host_id"), str):
            audit.record_node(task_id, "triage", input={"alert_id": raw.get("alert_id")}, output={"error": WF_3001_ANALYSIS_OUTPUT_INVALID}, elapsed_ms=_elapsed_ms(started))
            return {
                **_halt(
                    "escalated",
                    WF_3001_ANALYSIS_OUTPUT_INVALID,
                    f"告警结构化解析失败，输出非法: {(resp.content or '')[:200]}",
                ),
                "token_used": token_used,
            }
        if token_used > cfg.token_budget_default:
            audit.record_node(task_id, "triage", input={"alert_id": raw.get("alert_id")}, output={"error": WF_3003_TOKEN_BUDGET_EXCEEDED}, elapsed_ms=_elapsed_ms(started))
            return {
                **_halt("escalated", WF_3003_TOKEN_BUDGET_EXCEEDED, f"token 预算超限（{token_used} > {cfg.token_budget_default}）"),
                "token_used": token_used,
            }
        audit.record_node(task_id, "triage", input={"alert_id": raw.get("alert_id")}, output=parsed, elapsed_ms=_elapsed_ms(started))
        return {
            "alert": {**state["alert"], "structured": parsed},
            "token_used": token_used,
            "summaries": state["summaries"] + [f"triage: {parsed.get('summary', '')[:50]}"],
        }

    return triage


def make_collect(llm: LLMAdapter, registry: ToolRegistry, audit: AuditLogger, cfg: WorkflowConfig) -> Any:
    """构造 collect 节点：deep 档 function-calling 循环（≤3 轮），只暴露低危工具。

    Args:
        llm: LLM 适配器。
        registry: 工具注册表（自动过滤 risk=="low"）。
        audit: 审计器。
        cfg: 工作流配置。

    Returns:
        LangGraph 节点函数。
    """
    low_risk_schemas = [
        registry.get(name).to_openai_schema()
        for name in registry.names()
        if registry.get(name).risk == "low"
    ]

    def collect(state: DiagnosisState) -> dict[str, Any]:
        started = time.perf_counter()
        task_id = state["task_id"]
        alert = state["alert"].get("structured") or state["alert"]["raw"]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _COLLECT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"alert": alert, "known_evidence": state["evidence"]},
                    ensure_ascii=False,
                ),
            },
        ]
        evidence = list(state["evidence"])
        seen = {_evidence_signature(e["tool"], e["params"]) for e in evidence}
        token_used = state["token_used"]

        for _ in range(_COLLECT_MAX_TOOL_ROUNDS):
            if token_used > cfg.token_budget_default:
                break
            resp = llm.chat(messages, tier="deep", tools=low_risk_schemas)
            token_used += resp.usage.total_tokens
            audit.record_llm(
                task_id, "collect", tier="deep", model=resp.model, messages=messages,
                response_text=resp.content or "", prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens, elapsed_ms=_elapsed_ms(started),
            )
            if not resp.tool_calls:
                break
            # OpenAI 协议要求回写 assistant 消息（含 tool_calls），后续 tool 消息按 id 对应
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments_json},
                        }
                        for call in resp.tool_calls
                    ],
                }
            )
            for call in resp.tool_calls:
                try:
                    args = json.loads(call.arguments_json) if call.arguments_json else {}
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = registry.get(call.name).run(args)
                except OpsAgentError as exc:  # TOOL-1003 未注册等：作为工具错误回传模型
                    result = ToolResult.fail(exc.code, exc.message, 0.0)
                sig = _evidence_signature(call.name, args)
                if sig not in seen:
                    seen.add(sig)
                    evidence.append(
                        {"tool": call.name, "params": args, "data": result.data, "error_code": result.error_code}
                    )
                payload = {
                    "success": result.success,
                    "data": result.data,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                }
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(payload, ensure_ascii=False)}
                )
                audit.record_tool(task_id, "collect", call.name, params=args, result=result)
            if token_used > cfg.token_budget_default:
                break

        audit.record_node(
            task_id, "collect", round_no=state["round"],
            input={"evidence_count": len(state["evidence"])},
            output={"evidence_count": len(evidence), "token_used": token_used},
            elapsed_ms=_elapsed_ms(started),
        )
        if token_used > cfg.token_budget_default:
            return {
                **_halt("escalated", WF_3003_TOKEN_BUDGET_EXCEEDED, f"token 预算超限（{token_used} > {cfg.token_budget_default}）"),
                "evidence": evidence,
                "token_used": token_used,
            }
        return {
            "evidence": evidence,
            "token_used": token_used,
            "summaries": state["summaries"] + [f"collect: {len(evidence)} 条证据"],
        }

    return collect


def make_analyze(llm: LLMAdapter, audit: AuditLogger, cfg: WorkflowConfig) -> Any:
    """构造 analyze 节点：deep 档单轮直出根因与修复方案；输出非法 WF-3001。

    Args:
        llm: LLM 适配器。
        audit: 审计器。
        cfg: 工作流配置。

    Returns:
        LangGraph 节点函数。
    """

    def analyze(state: DiagnosisState) -> dict[str, Any]:
        started = time.perf_counter()
        task_id = state["task_id"]
        alert = state["alert"].get("structured") or state["alert"]["raw"]
        messages = [
            {"role": "system", "content": _ANALYZE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"alert": alert, "evidence": state["evidence"]}, ensure_ascii=False
                ),
            },
        ]
        try:
            resp = llm.chat(messages, tier="deep")
        except OpsAgentError as exc:
            audit.record_llm(
                task_id, "analyze", tier="deep", model="", messages=messages,
                error_code=exc.code, elapsed_ms=_elapsed_ms(started),
            )
            audit.record_node(task_id, "analyze", round_no=state["round"], input={"evidence_count": len(state["evidence"])}, output={"error": exc.code}, elapsed_ms=_elapsed_ms(started))
            return _halt("escalated", exc.code, exc.message)
        token_used = state["token_used"] + resp.usage.total_tokens
        audit.record_llm(
            task_id, "analyze", tier="deep", model=resp.model, messages=messages,
            response_text=resp.content or "", prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens, elapsed_ms=_elapsed_ms(started),
        )
        parsed = _parse_json_content(resp.content)
        if parsed is None or not parsed.get("fix_action") or "root_cause" not in parsed:
            audit.record_node(task_id, "analyze", round_no=state["round"], input={"evidence_count": len(state["evidence"])}, output={"error": WF_3001_ANALYSIS_OUTPUT_INVALID}, elapsed_ms=_elapsed_ms(started))
            return {
                **_halt(
                    "escalated",
                    WF_3001_ANALYSIS_OUTPUT_INVALID,
                    f"根因分析输出非法（缺 root_cause/fix_action）: {(resp.content or '')[:200]}",
                ),
                "token_used": token_used,
            }
        if token_used > cfg.token_budget_default:
            audit.record_node(task_id, "analyze", round_no=state["round"], input={"evidence_count": len(state["evidence"])}, output={"error": WF_3003_TOKEN_BUDGET_EXCEEDED}, elapsed_ms=_elapsed_ms(started))
            return {
                **_halt("escalated", WF_3003_TOKEN_BUDGET_EXCEEDED, f"token 预算超限（{token_used} > {cfg.token_budget_default}）"),
                "token_used": token_used,
            }
        audit.record_node(
            task_id, "analyze", round_no=state["round"],
            input={"evidence_count": len(state["evidence"])},
            output={"root_cause": parsed["root_cause"], "fix_action": parsed["fix_action"]},
            elapsed_ms=_elapsed_ms(started),
        )
        return {
            "root_cause": {"desc": parsed["root_cause"], "confidence": parsed.get("confidence")},
            "fix_plan": {"action": parsed["fix_action"], "params": parsed.get("fix_params") or {}},
            "token_used": token_used,
            "summaries": state["summaries"] + [f"analyze: {parsed['root_cause'][:50]}"],
        }

    return analyze


def make_execute(registry: ToolRegistry, audit: AuditLogger) -> Any:
    """构造 execute 节点：执行修复动作（高危直通，双保险 P2 接入治理层）。

    Args:
        registry: 完整工具注册表（含高危修复工具）。
        audit: 审计器。

    Returns:
        LangGraph 节点函数。
    """

    def execute(state: DiagnosisState) -> dict[str, Any]:
        started = time.perf_counter()
        task_id = state["task_id"]
        plan = state["fix_plan"]
        assert plan is not None  # 图路由保证：analyze 合法输出后才进入 execute
        params = {**plan["params"], "request_id": f"{task_id}-r{state['round']}"}
        try:
            tool = registry.get(plan["action"])
        except OpsAgentError as exc:  # TOOL-1003 未注册动作 → 转人工
            audit.record_node(task_id, "execute", round_no=state["round"], input={"action": plan["action"]}, output={"error": exc.code}, elapsed_ms=_elapsed_ms(started))
            return _halt("escalated", exc.code, exc.message)
        result = tool.run(params)
        audit.record_tool(task_id, "execute", plan["action"], params=params, result=result)
        if not result.success:
            audit.record_node(task_id, "execute", round_no=state["round"], input={"action": plan["action"]}, output={"error": result.error_code}, elapsed_ms=_elapsed_ms(started))
            return _halt(
                "escalated",
                result.error_code or "TOOL-1001",
                result.error_message or "修复执行失败",
            )
        audit.record_node(
            task_id, "execute", round_no=state["round"],
            input={"action": plan["action"]}, output=result.data,
            elapsed_ms=_elapsed_ms(started),
        )
        return {
            "fix_result": result.data,
            "summaries": state["summaries"] + [f"execute: {plan['action']} → matched={result.data.get('matched')}"],
        }

    return execute


def make_validate(
    llm: LLMAdapter,
    recovery_tool: BaseTool,
    audit: AuditLogger,
    cfg: WorkflowConfig,
) -> Any:
    """构造 validate 节点：check_recovery 客观判定 → 闭环生成报告 / 未恢复进入下一轮或 WF-3002。

    Args:
        llm: LLM 适配器（报告生成为 fast 档）。
        recovery_tool: 恢复判定工具（工作流内部持有，不在 LLM 工具集中）。
        audit: 审计器。
        cfg: 工作流配置（max_rounds 为二次诊断轮次上限）。

    Returns:
        LangGraph 节点函数。
    """

    def validate(state: DiagnosisState) -> dict[str, Any]:
        started = time.perf_counter()
        task_id = state["task_id"]
        host_id = state["alert"]["structured"]["host_id"]
        result = recovery_tool.run({"host_id": host_id})
        audit.record_tool(task_id, "validate", recovery_tool.name, params={"host_id": host_id}, result=result)
        recovery_data = result.data or {}

        if result.success and recovery_data.get("recovered"):
            report_messages = [
                {"role": "system", "content": _REPORT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "alert": state["alert"].get("structured"),
                            "root_cause": state["root_cause"],
                            "fix": {"action": state["fix_plan"]["action"], "result": state["fix_result"]},
                            "recovery": recovery_data,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            resp = llm.chat(report_messages, tier="fast")
            token_used = state["token_used"] + resp.usage.total_tokens
            audit.record_llm(
                task_id, "validate", tier="fast", model=resp.model,
                messages=report_messages,
                response_text=resp.content or "",
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                elapsed_ms=_elapsed_ms(started),
            )
            audit.record_node(
                task_id, "validate", round_no=state["round"],
                input={"host_id": host_id}, output={"recovered": True, "status": "closed"},
                elapsed_ms=_elapsed_ms(started),
            )
            return {
                "recovery": recovery_data,
                "report": {"text": resp.content or "", "recovery": recovery_data},
                "token_used": token_used,
                "summaries": state["summaries"] + ["validate: recovered, closed"],
                "status": "closed",
                "error": None,
            }

        new_round = state["round"] + 1
        if new_round >= cfg.max_rounds:
            audit.record_node(
                task_id, "validate", round_no=new_round,
                input={"host_id": host_id}, output={"recovered": False, "error": WF_3002_MAX_ROUNDS_EXCEEDED},
                elapsed_ms=_elapsed_ms(started),
            )
            return {
                "recovery": recovery_data,
                "round": new_round,
                **_halt(
                    "escalated",
                    WF_3002_MAX_ROUNDS_EXCEEDED,
                    f"恢复未达成，轮次超限（{new_round}/{cfg.max_rounds}），转人工处理",
                ),
            }
        audit.record_node(
            task_id, "validate", round_no=new_round,
            input={"host_id": host_id}, output={"recovered": recovery_data.get("recovered"), "next": "analyze"},
            elapsed_ms=_elapsed_ms(started),
        )
        return {
            "recovery": recovery_data,
            "round": new_round,
            "summaries": state["summaries"] + [f"validate: not recovered, round {new_round}"],
        }

    return validate


__all__ = [
    "make_analyze",
    "make_collect",
    "make_execute",
    "make_triage",
    "make_validate",
]
