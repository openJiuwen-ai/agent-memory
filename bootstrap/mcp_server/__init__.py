"""MCP surface — 一个真正的 Model Context Protocol 服务，把记忆动词暴露为 MCP 工具。

与 ``bootstrap/http_server``（HTTP）、``bootstrap/cli``（CLI）平级的协议适配器：复用同一套
已装配内核 + ``handler.dispatch``，自身不含业务逻辑，只做 MCP 协议（JSON-RPC over stdio /
Streamable HTTP）与内核 verb 之间的翻译（A §15「一个内核，多形态接入」）。

需要 mcp SDK：``pip install ".[mcp]"``。
"""
