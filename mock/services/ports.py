"""mock 四服务端口常量（与 src/opsagent/config.py MockConfig 镜像；mock 域不得 import 业务代码）。"""

HOST = "127.0.0.1"
MONITOR_PORT = 8101
LOGS_PORT = 8102
CMDB_PORT = 8103
TICKET_PORT = 8104

__all__ = ["CMDB_PORT", "HOST", "LOGS_PORT", "MONITOR_PORT", "TICKET_PORT"]
