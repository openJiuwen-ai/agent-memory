# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI surface for the agent-memory memory engine.

A protocol adapter peer to ``jiuwen_memory_entry/http_server`` (the HTTP surface): commands and
parameter validation are derived from the ``MemoryAPI`` contract
(``jiuwen_memory_entry/core/api_contract``); local execution reuses the assembled kernel via
``invoke_api`` and adds no business logic.
"""
