"""具体后端实现共用的小工具：异常归一、scope 派生与 SSL 配置读取。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, NamedTuple
from urllib.parse import parse_qs, urlparse

from common.errors import AgentMemoryError, BackendError, ValidationError
from common.factory.factory import Factory
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


# -- SSL：统一配置读取，各后端自行翻译成客户端参数 ------------------------------ #


class SslConfig(NamedTuple):
    """``ssl_verify`` 开关与 ``ssl_ca_cert`` 证书路径的读取结果。"""

    verify: bool
    ca_cert: str | None


def as_bool(value: Any, *, default: bool) -> bool:
    """把配置值归一为布尔。

    配置经 ``${VAR:-false}`` 展开后是**字符串** ``"false"``，直接 ``if`` 判定为真，
    故必须显式归一。取值集合与 common.security 层保持一致。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def read_ssl_config(config, *, backend: str) -> SslConfig:
    """读本组件的 ``ssl_verify`` / ``ssl_ca_cert``。

    ``ssl_verify`` 只管**是否校验服务端证书**，不负责开启加密——加密开关落在连接串
    上（``rediss://`` / ``https://`` / ``sslmode=``），各后端形式不同，见各 builder。

    默认关闭：不配置时行为与引入本参数前完全一致，现有明文部署不受影响。开启但未给
    证书即在**装配阶段**报错——云厂商多为自签 CA，缺证书时客户端会拿系统 CA 去校验并
    失败，那个报错指向证书链、看不出是配置漏项，故在此提前拦截。
    """
    verify = as_bool(Factory.cfg_get(config, "ssl_verify"), default=False)
    ca_cert = (Factory.cfg_get(config, "ssl_ca_cert") or "").strip() or None
    if verify and not ca_cert:
        raise ValidationError(
            f"{backend} 配置了 ssl_verify=true，必须同时提供 params.ssl_ca_cert"
        )
    return SslConfig(verify=verify, ca_cert=ca_cert)


def require_tls_scheme(value: Any, *, expected: str, backend: str, param: str) -> None:
    """校验连接串已声明 TLS scheme，否则装配阶段报错。

    redis 与 elasticsearch 的加密开关**只存在于 scheme**（实测：redis-py 的
    ``ssl=True`` 不生效，仅 ``rediss://`` 会切到 SSLConnection；elasticsearch-py 8.x
    已移除 ``use_ssl``）。若此处放行，证书参数会被传入却不生效，连接以明文建立而
    调用方以为已加密——静默失败比报错危险，故拦在装配期。

    ``value`` 可为单个地址或地址列表（elasticsearch 的 ``hosts`` 支持多节点），
    列表逐个校验——整体转字符串会把合法的多节点配置一并判错。
    """
    candidates = value if isinstance(value, (list, tuple)) else [value]
    for item in candidates:
        if not str(item).startswith(f"{expected}://"):
            raise ValidationError(
                f"{backend} 配置了 ssl_verify=true，params.{param} 必须使用 "
                f"{expected}:// scheme（当前值 {item!r}）"
            )


def reject_url_tls_params(value: Any, *, backend: str, param: str) -> None:
    """``ssl_verify`` 为真时，连接串不得自带 ``ssl_*`` 查询参数。

    redis-py 的 ``from_url`` 让 URL query **覆盖** kwargs（实测），故连接串里的
    ``?ssl_cert_reqs=none`` / ``?ssl_check_hostname=false`` 会静默关掉校验，而配置
    仍声称 ssl_verify=true——调用方以为受保护、实际未校验对端身份，比明文更危险。
    显式回传 ``ssl_cert_reqs="required"`` 也压不住（同样被 URL 覆盖），只能拒绝。

    ``ssl_verify=false`` 时不调用本函数：那是把 TLS 完全交给连接串自理的逃生舱。
    """
    candidates = value if isinstance(value, (list, tuple)) else [value]
    for item in candidates:
        query = urlparse(str(item)).query
        conflicts = sorted(key for key in parse_qs(query) if key.startswith("ssl_"))
        if conflicts:
            raise ValidationError(
                f"{backend} 配置了 ssl_verify=true，params.{param} 不得再带 TLS 查询参数 "
                f"{conflicts}——URL 参数会覆盖此处设置并可能关闭校验；"
                f"请改用 ssl_verify / ssl_ca_cert，或置 ssl_verify=false 由 URL 全权负责"
            )
