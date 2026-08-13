"""按 ConfigSource 解析 ``*.active`` 与单字段晚绑定值。

- :func:`resolve_active_name`：异质多实例次选路径（未知 active 抛 ValidationError）
- :func:`resolve_bound_value`：同实例字段晚绑定（model/api_key/url 等优先路径）
"""

from __future__ import annotations

from collections.abc import Sequence

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config.config_source import ConfigSource
from jiuwen_memory.config.keys import active_key, namespaced_key


def resolve_active_name(
    config_source: ConfigSource,
    *,
    namespace: str,
    available: Sequence[str],
    default: str,
) -> str:
    """读取 ``<namespace>.active``；缺失则用 ``default``；未知实例名抛 ValidationError。

    禁止静默回退到错误实例（S08 多实例切换契约）。
    """
    available_set = {str(name) for name in available}
    if default not in available_set:
        raise ValidationError(
            f"{namespace}.active default {default!r} 不在已预装实例中："
            f"{sorted(available_set)}"
        )

    raw = config_source.fetch(active_key(namespace))
    if raw is None or str(raw).strip() == "":
        return default

    name = str(raw).strip()
    if name not in available_set:
        raise ValidationError(
            f"{namespace}.active={name!r} 指向未预装实例（已预装："
            f"{sorted(available_set)}）"
        )
    return name


def resolve_bound_value(
    config_source: ConfigSource | None,
    *,
    namespace: str,
    field: str,
    fallback: str | None = None,
) -> str | None:
    """读取 ``<namespace>.<field>``（如 ``llm.model``）；缺失返回 ``fallback``。

    供 Embedder/LLM/Reranker/Store 在调用路径晚绑定凭证与连接串。
    对某实现无意义的字段由消费方忽略（S08）。
    """
    if config_source is None:
        return fallback
    live = config_source.fetch(namespaced_key(namespace, field))
    if live is None or str(live).strip() == "":
        return fallback
    return str(live)
