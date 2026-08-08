"""DictConfigSource — 可变内存配置源（测试 / 简易产品落地）。

产品可将配置中心拉取结果写入本源，或在本源之上做 Overlay。
``put`` 供运行时更新；不属于 MemoryAPI 业务入参路径。
"""

from __future__ import annotations

from config.config_source import ConfigSource, ConfigSourceProducer
from config.project import project_assembly_values


class DictConfigSource(ConfigSource):
    """可变 dict；``fetch`` 读当前表，``put`` 更新单键。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = {str(k): str(v) for k, v in (values or {}).items()}

    def fetch(self, key: str) -> str | None:
        """按稳定 key 取值；缺失返回 ``None``。"""
        if key not in self._values:
            return None
        return self._values[key]

    def put(self, key: str, value: str) -> None:
        """运行时更新一个配置键（立即对后续 fetch 可见）。

        供产品/配置中心侧写入；**不属于** MemoryAPI ``write``/``recall`` 业务入参路径。
        """
        self._values[str(key)] = str(value)

    def health(self) -> None:
        """内存表无外部依赖，恒健康。"""
        return None

    def as_dict(self) -> dict[str, str]:
        """返回内部表的浅拷贝。"""
        return dict(self._values)


@ConfigSourceProducer.register("dict")
def _build_dict(config) -> DictConfigSource:
    """以装配投影为底，再用 params.values 覆盖（便于只改 active 等少量键）。"""
    projected = project_assembly_values(config.ctx)
    extra = config.get("values") or {}
    if isinstance(extra, dict):
        projected.update({str(k): str(v) for k, v in extra.items()})
    return DictConfigSource(projected)
