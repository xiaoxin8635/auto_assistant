# OpsAgent AI 开发编码规约（v1.0）

> 适用范围：本仓库所有 AI 生成与人工编写的代码。AI 编码会话开始前必须加载本文件。
> 与本规约冲突的临时指令，须在对话中明确标注"例外"并说明理由，否则一律按本规约执行。

---

## 1. 技术栈锁定（禁止引入未约定依赖）

| 层 | 锁定 | 版本 | 备注 |
| --- | --- | --- | --- |
| 语言/运行时 | Python | **3.11**（uv 管理，`.venv`） | 3.12+ 禁用 |
| 工作流 | langgraph | 1.2.19 | interrupt/Checkpointer 核心，升级需跑全量场景回归 |
| LLM 接入 | openai | 3.42.0 | 通义千问/DeepSeek 统一走 OpenAI 兼容协议；**禁止**引入 dashscope 或厂商深度绑定 |
| 向量库 | chromadb | 1.5.9 | 仅经 `storage/` 适配层访问 |
| 嵌入 | sentence-transformers | 6.2.0 | 模型固定 bge-small-zh-v1.5 |
| Web 服务 | fastapi | 0.162.0 | 模拟业务系统与演示 Web |
| HTTP 客户端 | httpx | 0.28.1 | 工具调用统一用 httpx，**禁止 requests** |
| 校验 | pydantic / pydantic-settings | 2.14.2 | 所有入参出参 schema |
| 配置 | pyyaml / python-dotenv | — | — |
| 测试 | pytest | — | — |

**新增依赖流程**：说明用途 → 项目所有者确认 → 加入 requirements.txt（附注释）→ 更新 requirements-lock.txt。
AI 不得私自 `pip install` 并 import 未约定的包。pip 源：清华镜像。

---

## 2. 目录结构与依赖方向

以 DESIGN.md §6 目录结构为准。依赖方向强制单向：

```
access → workflow → tools / rag → storage
governance（横切，经接口注入，不反向依赖业务）
mock/（独立进程，不 import src/opsagent 业务代码）
tests/（目录结构镜像 src/）
```

- 禁止跨层直连：workflow 不许直接 `import chromadb`，必须经 storage 适配层
- 模块内禁止循环导入；公共接口放 `__init__.py` 或 `interfaces.py`

---

## 3. 命名规范

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 包/模块/函数/变量 | snake_case | `collect_evidence()` |
| 类 | PascalCase | `ScenarioEngine` |
| 常量 | UPPER_SNAKE_CASE | `MAX_ROUNDS = 15` |
| 私有成员 | 前缀 `_` | `_inject_fault()` |
| 文件名 | snake_case.py，禁止中文/拼音 | `tool_wrapper.py` |
| 数据库表/字段 | snake_case，表名复数 | `tasks` / `node_runs` / `llm_calls` / `tool_calls` |
| 场景 ID | `SC-<类型>-<序号>` | `SC-CPU-001` |

**领域词汇必须对齐 [GLOSSARY.md](./GLOSSARY.md)**，代码标识符用对应英文：
alert / ticket / incident / evidence / hypothesis / root_cause / remediation / recovery_check / escalation / closure。禁止同义混用（如 ticket/order、remediation/fix_command 混用）。

---

## 4. 注释与类型规范（Google style docstring，中文）

**三必须**：

1. 每个文件头部必须有模块级 docstring（一句话职责 + 关键约束）
2. 所有公共类与公共函数必须 docstring：`Args / Returns / Raises`（无参数的写 `Args: 无`）
3. 复杂逻辑（阈值来源、workaround、并发约束）必须行内 `#` 注释，**讲 why 不讲 what**

**类型注解**：所有公共函数/方法完整注解（mypy strict 兼容）；私有函数鼓励注解。

**禁止**：注释掉的死代码（直接删）；空洞注释（"获取数据"这类复述代码）；无归属的 TODO（格式：`TODO(名字): 事项`）。

---

## 5. 错误处理规范

### 5.1 异常体系与错误码

- 基类 `OpsAgentError(Exception)`：字段 `code / message / detail`
- 错误码 = 模块段-4 位数字，常量集中 `errors.py`：

| 模块 | 段 | 示例 |
| --- | --- | --- |
| TOOL 工具层 | 1xxx | TOOL-1001 参数校验失败 |
| WF 工作流 | 3xxx | WF-3001 轮次超限 |
| MOCK 模拟服务 | 4xxx | MOCK-4002 注入延迟 |
| GOV 治理 | 5xxx | GOV-5001 预算熔断 |
| RAG | 6xxx | RAG-6001 知识库无命中 |
| ACC 接入层 | 7xxx | ACC-7001 告警格式非法 |
| STG 存储 | 8xxx | STG-8001 checkpoint 写入失败 |

### 5.2 分层策略

| 层 | 策略 |
| --- | --- |
| 工具层 | **永不向 LLM 抛异常**。统一返回 `ToolResult(ok, data | error{code, message, hint})`，`hint` 告诉模型下一步该做什么 |
| 服务层（FastAPI） | 全局 `exception_handler` 统一响应体 `{"code", "message", "detail", "request_id"}`；HTTP 映射：校验失败 422 / 不存在 404 / 冲突 409 / 内部 500 |
| 工作流节点 | 捕获 `OpsAgentError` → 决策 escalate / failed 并写审计；**禁止**裸 `except:`、`except: pass`、吞错继续执行 |
| 重试 | 仅在 httpx 传输层（超时/5xx/429），指数退避 ≤2 次；业务失败不自动重试（由工作流决策） |

---

## 6. 日志规范

- 用 `logging`，**禁止 print**
- 格式：`%(asctime)s | %(levelname)-8s | %(name)s | task=%(task_id)s | %(message)s`
  （task_id 经 `extra` 注入 LoggerAdapter，无任务上下文时为 `-`）
- 分级：

| 级别 | 场景 |
| --- | --- |
| DEBUG | 开发调试细节 |
| INFO | 节点进入/退出（含耗时）、LLM 调用（模型+token 数）、工具调用结果、工单状态变更 |
| WARNING | 重试发生、降级、注入命中、预算接近阈值 |
| ERROR | 转 escalate / failed、熔断触发、未预期异常 |

**必须埋点清单**（缺一不合格）：节点进出、LLM 调用、工具调用、审核挂起/恢复、终态、熔断。
**敏感过滤**：password / key / token / secret 一律不落日志（filter 统一处理）。
**输出**：控制台 INFO；`logs/app.log` DEBUG，按天滚动保留 7 天。

---

## 7. 配置管理规范

- 分层文件：`config/model.yaml`（真实密钥，gitignore）+ `config/model.example.yaml`（模板，入库，无密钥）
- 代码只经 pydantic-settings 配置类读取，优先级：环境变量 > yaml > 默认值
- 配置类分组：`ModelConfig / ToolConfig / WorkflowConfig / MockConfig / EvalConfig`；`get_settings()` lru_cache 单例
- **禁止魔法值**：超时/预算/阈值/端口/路径全部进配置类或 `constants.py`；代码中出现新魔法值视为缺陷
- 密钥红线：代码/日志/测试/示例中出现真实 key 属最高级缺陷，立即轮换并修复

---

## 8. Git 与 AI 协作规约

- 提交信息：Conventional Commits，中文描述（`feat: 支持告警字段结构化提取` / `fix: 修复审核恢复后轮次计数丢失`）
- **完成的定义**（Definition of Done）：代码 + 类型注解 + docstring + 日志埋点 + pytest 用例全部通过
- **禁止**：
  - 虚假实现（占位函数不标注、假装已调用、编造返回值）
  - 跳过测试 / 删测试换绿灯
  - 私自引入依赖、修改配置结构
  - 修改已接受的 ADR（有异议走新 ADR，编号顺延）
- 修改公共接口（工具 schema / 状态机 / 表结构 / 错误码）必须同步：GLOSSARY、DESIGN、REQUIREMENTS 对应章节 + 相关测试
