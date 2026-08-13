"""跨层共用的小工具：配置值归一、TLS 配置读取与校验、scope 命名空间渲染、异常归一。

出站客户端（LLM / Embedder / Reranker）连的是 HTTP API，与 storage 连托管实例的
情形不同：公网端点由公共 CA 签发，系统 CA 即可校验；私有部署多为自签，须显式给出
CA 路径。故 ``ssl_verify=true`` 时**不强制**要求证书——缺证书回落系统 CA，而
storage 侧缺证书直接报错（云厂商一律私有 CA，缺证书必然失败）。两处矩阵的差异见
docs/features/common/F05-model-service-ssl.md。

``SCOPE_DIMS`` / ``scope_segments`` / ``wrap_backend`` / ``read_ssl_config`` /
``reject_url_tls_params`` 原在 ``storage/_support.py``，因 ``common`` 不能反向依赖
``storage`` 而下沉至此（``common/lock`` 与各存储后端共用）；``storage/_support.py``
继续再导出，原有 import 路径不变。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, NamedTuple
from urllib.parse import parse_qs, urlparse

from jiuwen_memory.common.errors import AgentMemoryError, BackendError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

SCOPE_DIMS = ("org", "space", "user", "agent", "session")


def as_bool(value: Any, *, default: bool) -> bool:
    """把配置值归一为布尔。

    配置经 ``${VAR:-false}`` 展开后是**字符串** ``"false"``，直接 ``if`` 判定为真，
    故必须显式归一。``None`` 表示未配置，取 ``default``。
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


class SslConfig(NamedTuple):
    """``ssl_verify`` 开关与 ``ssl_ca_cert`` 证书路径的读取结果。

    storage 侧从组件 ``params`` 读、出站客户端从全局配置按 ``<prefix>_`` 前缀读，
    结构一致，故共用本类型。
    """

    verify: bool
    ca_cert: str | None


def build_ssl_config(verify: Any, ca_cert: Any) -> SslConfig:
    """把两个原始配置值归一为 :class:`SslConfig`。

    两处取值方式不同（storage 走 ``Factory.cfg_get``、出站客户端走带前缀的
    ``config.get``），但归一规则一致：``${VAR:-false}`` 展开是字符串须过 ``as_bool``，
    ``${VAR:-}`` 展开是空串须归一为 ``None`` 而非空路径。
    """
    return SslConfig(
        verify=as_bool(verify, default=False),
        ca_cert=(ca_cert or "").strip() or None,
    )


def read_outbound_ssl(config: Any, prefix: str) -> SslConfig:
    """读某个出站客户端的 SSL 配置（全局命名空间下的 ``<prefix>_ssl_*``）。

    默认关闭：不配置时完全不干预客户端，行为与引入本参数前一致——``http://`` 明文
    直连（开发自测），``https://`` 仍走 SDK 默认的公共 CA 校验。开启后才接管信任锚。
    """
    return build_ssl_config(
        config.get(f"{prefix}_ssl_verify"), config.get(f"{prefix}_ssl_ca_cert")
    )


def require_tls_scheme(
    value: Any,
    *,
    expected: str,
    component: str,
    param: str,
    allow_empty: bool = False,
) -> None:
    """校验连接串已声明 TLS scheme，否则装配阶段报错。

    加密开关只存在于 scheme——若此处放行，证书参数会被传入却不生效，连接以明文建立
    而调用方以为已加密。静默失败比报错危险，故拦在装配期。（实测：redis-py 的
    ``ssl=True`` 不生效、仅 ``rediss://`` 会切到 SSLConnection；elasticsearch-py 8.x
    已移除 ``use_ssl``。）

    ``value`` 可为单个地址或地址列表（elasticsearch 的 ``hosts`` 支持多节点），列表
    逐个校验——整体转字符串会把合法的多节点配置一并判错。``allow_empty`` 供出站
    客户端使用：``base_url`` 未配置时回落 SDK 内置端点，官方 API 均为 https。
    """
    candidates = value if isinstance(value, (list, tuple)) else [value]
    for item in candidates:
        text = str(item or "").strip()
        if not text and allow_empty:
            continue
        if not text.startswith(f"{expected}://"):
            raise ValidationError(
                f"{component} 配置了 ssl_verify=true，{param} 必须使用 "
                f"{expected}:// scheme（当前值 {item!r}）"
            )


def require_https(base_url: Any, *, component: str, param: str) -> None:
    """出站客户端的 scheme 校验：``base_url`` 必须是 https（未配置时放行）。"""
    require_tls_scheme(
        base_url,
        expected="https",
        component=component,
        param=f"{param}_base_url",
        allow_empty=True,
    )


def require_ca_file(ca_cert: str | None, *, component: str, param: str) -> None:
    """校验证书文件存在，否则装配阶段报错。

    httpx 在构造客户端时立即加载证书，路径写错只会抛出不带上下文的
    ``FileNotFoundError``；容器内路径与宿主机路径写混是常见配置错误，故在此
    先行拦截并指明是哪个组件的哪个参数。
    """
    if not ca_cert:
        return
    if not os.path.isfile(ca_cert):
        raise ValidationError(
            f"{component} 的 {param}_ssl_ca_cert 指向的文件不存在：{ca_cert}"
            f"（容器部署时须为容器内路径，并确认已挂载）"
        )


def outbound_verify(ca_cert: str | None) -> bool | str:
    """把 CA 证书路径翻译成 httpx/requests 的 ``verify`` 取值。

    有证书用路径覆盖信任锚，没有则 ``True`` 走系统 CA——公网端点由公共 CA 签发，
    这是正常状态，不视作配置缺失。入参接受原始配置值（可能是未归一的空串）。
    """
    return (ca_cert or "").strip() or True


def read_ssl_config(config: Any, *, backend: str) -> SslConfig:
    """读本组件 ``params`` 下的 ``ssl_verify`` / ``ssl_ca_cert``。

    ``ssl_verify`` 只管**是否校验服务端证书**，不负责开启加密——加密开关落在连接串
    上（``rediss://`` / ``https://`` / ``sslmode=``），各后端形式不同，见各 builder。

    默认关闭：不配置时行为与引入本参数前完全一致，现有明文部署不受影响。开启但未给
    证书即在**装配阶段**报错——云厂商多为自签 CA，缺证书时客户端会拿系统 CA 去校验并
    失败，那个报错指向证书链、看不出是配置漏项，故在此提前拦截。

    这是与出站 HTTP 客户端（:func:`read_outbound_ssl`）的唯一矩阵差异：后者缺证书
    回落系统 CA 而非报错。
    """
    ssl = build_ssl_config(
        Factory.cfg_get(config, "ssl_verify"), Factory.cfg_get(config, "ssl_ca_cert")
    )
    if ssl.verify and not ssl.ca_cert:
        raise ValidationError(
            f"{backend} 配置了 ssl_verify=true，必须同时提供 params.ssl_ca_cert"
        )
    return ssl


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


# -- scope 命名空间与后端异常归一 ------------------------------------------------ #


def scope_segments(scope: Scope) -> list[str]:
    """把 scope 渲染为定长五段（空维度用 ``_`` 占位），供 kv/fs/lock 做命名空间隔离。

    定长且占位可避免不同 scope 折叠到同一命名空间（如 ``org`` 空与 ``user`` 空
    错位拼接）；各段把路径分隔符替换掉以防越界。
    """
    return [(getattr(scope, dim) or "_").replace("/", "_").replace(":", "_") for dim in SCOPE_DIMS]


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
