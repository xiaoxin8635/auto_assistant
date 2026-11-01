# OpsAgent 项目协作规约（Claude 项目级指令）

## 强制加载

1. **编码任务开始前必须先读 [CODING_STANDARDS.md](./CODING_STANDARDS.md)**——技术栈锁定、命名、注释、错误处理、日志、配置规范，冲突时以它为准
2. 领域术语以 [GLOSSARY.md](./GLOSSARY.md) 为准，代码标识符与文档用词必须对齐
3. 架构决策见 `docs/adr/`，需求边界见 `docs/REQUIREMENTS.md`（本地文档，不入库）
4. **编码前强制需求拷问（grilling）**：任何新功能、接口变更、Bug 修复类编码任务，**动手写代码前必须先执行 grilling 流程**（分轮提问 + 每题附推荐答案），与用户达成共识后才允许写实现；共识未达成前禁止动手。仅机械性微改豁免（错别字、常量数值调整、单行修复、注释/文档补充）

## 环境与命令

- Python 3.11 由 uv 管理（`.venv/`），pip 源用清华镜像
- 启动任何服务前**先检查端口占用**（mock 服务 8101~8104），被占用先 `taskkill //f //pid <pid>` 再启动，不要直接关 shell
- 运行测试：`.venv/Scripts/python.exe -m pytest`
- 拉取 HF 模型走国内镜像：`HF_ENDPOINT=https://hf-mirror.com`

## 硬性约束

- 技术栈锁定见 CODING_STANDARDS §1：**禁止 AI 引入未约定依赖**，新增依赖须先确认
- 评测与性能数字只能来自真实运行（P3 评测后回填文档），**禁止预写虚构数据**
- 高危修复动作未经人工确认状态时，任何代码路径不得绕过工具层拦截（双保险机制）
- `docs/` 目录已被 gitignore：设计文档、ADR 保持本地，不上传
- 每次代码修改后同步更新：注释、GLOSSARY/DESIGN/REQUIREMENTS 中受影响的章节、相关测试
