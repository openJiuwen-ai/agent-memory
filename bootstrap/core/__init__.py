"""core — 各协议 surface 共享的应用核（与传输无关）。

``bootstrap/{http_server, mcp_server, cli, sdk}`` 都是薄传输适配器，统一依赖这里：

- :mod:`server`：``Server`` 基类——装配一次内核（config + api + 真源）并暴露 ``dispatch``；
- :mod:`handler`：verb→``MemoryAPI`` 的唯一 dispatch 表 + JSON 信封整形；
- :mod:`profiles`：``OFFLINE`` 基线 + ``load_config``（profile / 配置层叠加）；
- :mod:`config_loader`：``load_layer``——读 YAML/JSON 并展开 ``${VAR}`` / ``${VAR:-默认}``。

这些都不含业务逻辑（业务在 ``src/``），也不绑定任何具体协议；各 surface 把本目录置于
``sys.path`` 首位后以 flat-import 复用（``import server`` 等）。core 只依赖 ``src/api``。
"""
