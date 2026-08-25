"""OverlayConfigSource — primary 优先，缺失时回退 fallback。

典型用法：Dict（可写）叠在 YamlDefaults（装配默认）之上，产品只改少量运行时键。
"""

from __future__ import annotations

from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer


class OverlayConfigSource(ConfigSource):
    """``fetch``：primary 返回非 ``None`` 则用之，否则问 fallback。"""

    def __init__(self, primary: ConfigSource, fallback: ConfigSource) -> None:
        """初始化 OverlayConfigSource。

        Args:
            primary: 参数 primary（ConfigSource）。
            fallback: 参数 fallback（ConfigSource）。
        """
        self._primary = primary
        self._fallback = fallback

    def fetch(self, key: str) -> str | None:
        """先 primary 后 fallback；两者皆缺返回 ``None``。"""
        value = self._primary.fetch(key)
        if value is not None:
            return value
        return self._fallback.fetch(key)

    def health(self) -> None:
        """两侧都须健康。"""
        self._primary.health()
        self._fallback.health()


@ConfigSourceProducer.register("overlay")
def _build_overlay(config) -> OverlayConfigSource:
    """params.primary / params.fallback：引用名或内联 raw spec（经 Producer.dep）。"""
    primary = ConfigSourceProducer.dep(config, "primary", default="dict")
    fallback = ConfigSourceProducer.dep(config, "fallback", default="yaml_defaults")
    if not isinstance(primary, ConfigSource) or not isinstance(fallback, ConfigSource):
        from jiuwen_memory.common.errors import ValidationError

        raise ValidationError("overlay 的 primary/fallback 必须是 ConfigSource")
    return OverlayConfigSource(primary=primary, fallback=fallback)
