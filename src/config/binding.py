"""调用路径晚绑定辅助：从 ConfigSource 解析 model/api_key/base_url/连接串。

与 :mod:`config.active` 的分工：
- ``resolve_bound_value`` / 本模块：同实例改凭证与连接（优先路径，S08）
- ``resolve_active_name``：异质已预装实例互切（次选路径）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from config.active import resolve_bound_value
from config.keys import (
    FIELD_API_KEY,
    FIELD_BASE_URL,
    FIELD_MODEL,
    NS_EMBEDDER,
    NS_KV_STORE,
    NS_LLM,
    NS_RERANKER,
    namespaced_key,
)

if TYPE_CHECKING:
    from config.config_source import ConfigSource


@dataclass(frozen=True)
class BoundEndpoint:
    """远端调用所需的晚绑定端点字段（LLM / Embedder / API Reranker 共用）。

    Attributes:
        model: 当前应使用的模型名
        api_key: 当前 API Key（可为占位空串，由后端在真实请求时校验）
        base_url: 当前 API 根地址；``None`` 表示走实现默认官方端点
    """

    model: str
    api_key: str
    base_url: str | None


def resolve_endpoint(
    config_source: ConfigSource | None,
    *,
    namespace: str,
    fallback_model: str,
    fallback_api_key: str,
    fallback_base_url: str | None,
) -> BoundEndpoint:
    """解析 ``<ns>.model`` / ``api_key`` / ``base_url``；缺失回落构造期默认值。

    ``config_source`` 为 ``None`` 时直接返回 fallback（兼容未注入场景）。
    """
    model = resolve_bound_value(
        config_source,
        namespace=namespace,
        field=FIELD_MODEL,
        fallback=fallback_model,
    ) or fallback_model
    api_key = resolve_bound_value(
        config_source,
        namespace=namespace,
        field=FIELD_API_KEY,
        fallback=fallback_api_key,
    ) or ""
    raw_url = resolve_bound_value(
        config_source,
        namespace=namespace,
        field=FIELD_BASE_URL,
        fallback=fallback_base_url,
    )
    base_url = (raw_url or "").strip() or None
    return BoundEndpoint(model=model, api_key=api_key, base_url=base_url)


def resolve_connection_url(
    config_source: ConfigSource | None,
    *,
    namespace: str = NS_KV_STORE,
    field: str = "url",
    fallback: str | None,
) -> str | None:
    """解析 Store 连接串（如 ``kv_store.url``）；空串视作未配置并回落 ``fallback``。"""
    live = resolve_bound_value(
        config_source, namespace=namespace, field=field, fallback=fallback
    )
    if live is None:
        return None
    text = str(live).strip()
    return text or None


def endpoint_key(namespace: str, field: str) -> str:
    """``namespaced_key`` 的别名，便于调用方语义化书写。"""
    return namespaced_key(namespace, field)


# 常用命名空间再导出，便于实现侧少写字符串
NS = {
    "embedder": NS_EMBEDDER,
    "llm": NS_LLM,
    "reranker": NS_RERANKER,
    "kv_store": NS_KV_STORE,
}
