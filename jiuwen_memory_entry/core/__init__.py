# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""core — 各协议 surface 共享的应用核（与传输无关）。

``jiuwen_memory_entry/{http_server, mcp_server, cli, sdk}`` 都是薄传输适配器，统一依赖这里：

- :mod:`server`：``Server`` 基类——Access composition root，经 ``jiuwen_memory.api.assemble_runtime``
  装配内核（传入 dict），公开面只有 ``api`` / ``dispatch`` / lifecycle；
- :mod:`handler`：verb→``MemoryAPI`` 的唯一 dispatch 表 + JSON 信封整形；
- :mod:`profiles`：``OFFLINE`` 基线 + ``load_config``（profile / 配置层叠加）；
- :mod:`config_loader`：``load_layer``——读 YAML/JSON 并展开 ``${VAR}`` / ``${VAR:-默认}``。

这些都不含业务逻辑（业务在 ``jiuwen_memory/``），也不绑定任何具体协议；各 surface 通过本地启动脚本
的 ``PYTHONPATH`` 或 Docker editable 安装保证导入优先级，并以 flat-import 复用
（``import server`` 等）。内核依赖只使用 ``jiuwen_memory.api``。
"""
