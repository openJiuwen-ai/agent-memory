"""http_server — 内核装配与协议 surface 的落点。

:mod:`server` 提供基类 :class:`~server.Server`（装配内核 + 共享 dispatch），
各协议 surface 继承它并补上各自的传输层（``__main__`` 的 HTTP/socket 形态、
未来的 MCP 形态）。模块以 flat-import 方式被 CLI/入口加载（``import server`` /
``import handler`` / ``import profiles``），不通过本包 __init__。
"""
