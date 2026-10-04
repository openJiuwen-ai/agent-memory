# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP surface — 一个真正的 Model Context Protocol 服务，把记忆动词暴露为 MCP 工具。

与 ``jiuwen_memory_entry/http_server``（HTTP）、``jiuwen_memory_entry/cli``（CLI）平级的
协议适配器：与二者共享同一套契约校验与调用桥
（``jiuwen_memory_entry/core/api_contract`` 的 ``parse_request`` / ``invoke_api``）
和同一套已装配内核，自身不含业务逻辑，只做 MCP 协议（JSON-RPC over stdio /
Streamable HTTP）与内核 verb 之间的翻译（A §15「一个内核，多形态接入」）。

需要 mcp SDK：``pip install ".[mcp]"``。
"""
