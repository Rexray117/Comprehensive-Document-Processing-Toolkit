"""集成桥接层：把文件处理能力暴露成 Agent / harness 可消费的接口形态。

三种接法，按 harness 的能力选：

* ``mcp_server`` —— MCP stdio 服务器（支持 MCP 的宿主首选，配置一段 JSON 即可用）
* ``stdio_bridge`` —— 单行 JSON 请求/响应（自定义 harness、subprocess 工具执行器）
* ``spec`` —— 工具契约生成（把动作声明导出成 function-calling / LangChain 工具定义）
"""

from __future__ import annotations

from bridges import spec

__all__ = ["spec"]
