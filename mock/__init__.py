"""OpsAgent 模拟域（mock）：场景引擎 + 四个模拟业务服务。

硬性规约：本包及子包**禁止 import src/opsagent 任何业务代码**——
对业务侧而言本域是"第三方系统"，只通过 HTTP 暴露。
"""
