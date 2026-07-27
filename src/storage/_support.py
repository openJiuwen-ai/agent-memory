"""具体后端实现共用的小工具：异常归一与 scope 派生。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from common.errors import AgentMemoryError, BackendError
from common.type_def import Scope

_DIMS = ("org", "space", "user", "agent", "session")


@contextmanager
def wrap_backend(action: str) -> Iterator[None]:
    """把后端 I/O 中的非预期异常归一为 :class:`~common.errors.BackendError`。

    本系统的业务异常（``ConflictError`` / ``NotFoundError`` 等，均为
    :class:`~common.errors.AgentMemoryError` 子类）原样透传——它们是接口契约的一部分，
    由实现按语义主动抛出；其余一切（网络、IO、客户端库内部错误、依赖缺失等）
    统一包成 ``BackendError``，让调用方跨后端用同一套捕获。
    """
    try:
        yield
    except AgentMemoryError:
        raise
    except Exception as exc:  # 适配层刻意兜底所有后端异常
        raise BackendError(f"{action}: {exc}") from exc


def scope_dims(scope: Scope) -> list[tuple[str, str]]:
    """返回 scope 中**非空**的维度 ``(dim, value)`` 列表。

    用于检索型后端构造 scope 过滤：org>space>user/agent>session 的层级语义下，
    只对非空维度施加等值约束（空维度 = 不限定该层）。``space`` 例外：只要
    ``org`` 已给出，即便 ``space`` 为空也会下推 ``space == ""``，避免空
    space 请求跨到其他 space。从而 ``scope`` 越具体、
    检索范围越窄，实现原生的多租户隔离与层级包含。
    """
    out: list[tuple[str, str]] = []
    for dim in _DIMS:
        value = getattr(scope, dim)
        include_dimension = bool(value) or (dim == "space" and bool(scope.org))
        if include_dimension:
            out.append((dim, value))
    return out


def scope_segments(scope: Scope) -> list[str]:
    """把 scope 渲染为定长五段（空维度用 ``_`` 占位），供 kv/fs 做命名空间隔离。

    定长且占位可避免不同 scope 折叠到同一命名空间（如 ``org`` 空与 ``user`` 空
    错位拼接）；各段把路径分隔符替换掉以防越界。
    """
    return [(getattr(scope, dim) or "_").replace("/", "_").replace(":", "_") for dim in _DIMS]
