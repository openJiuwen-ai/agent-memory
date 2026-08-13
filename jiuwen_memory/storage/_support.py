"""具体后端实现共用的小工具：scope 过滤派生，及从 common 再导出的公共件。

异常归一（``wrap_backend``）、scope 命名空间渲染（``scope_segments``）与 SSL 配置
读取校验（``read_ssl_config`` / ``reject_url_tls_params``）已下沉到
``common/_support.py``——``common/lock`` 也要用，而 common 不能反向依赖 storage。
此处保留再导出，各后端 import 路径不变。
"""

from __future__ import annotations

from jiuwen_memory.common._support import (  # noqa: F401  向后兼容的再导出
    SCOPE_DIMS,
    SslConfig,
    as_bool,
    build_ssl_config,
    read_ssl_config,
    reject_url_tls_params,
    require_tls_scheme,
    scope_segments,
    wrap_backend,
)
from jiuwen_memory.common.type_def import Scope


def scope_dims(scope: Scope) -> list[tuple[str, str]]:
    """返回 scope 中**非空**的维度 ``(dim, value)`` 列表。

    用于检索型后端构造 scope 过滤：org>space>user/agent>session 的层级语义下，
    只对非空维度施加等值约束（空维度 = 不限定该层）。``space`` 例外：只要
    ``org`` 已给出，即便 ``space`` 为空也会下推 ``space == ""``，避免空
    space 请求跨到其他 space。从而 ``scope`` 越具体、
    检索范围越窄，实现原生的多租户隔离与层级包含。

    与 ``scope_segments`` 的区别：本函数是检索侧的过滤构造（只约束非空维度），
    前者是存储侧的命名空间渲染（定长五段、空维度占位），两者不可互换。
    """
    out: list[tuple[str, str]] = []
    for dim in SCOPE_DIMS:
        value = getattr(scope, dim)
        include_dimension = bool(value) or (dim == "space" and bool(scope.org))
        if include_dimension:
            out.append((dim, value))
    return out
