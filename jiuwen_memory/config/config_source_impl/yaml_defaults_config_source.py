"""YamlDefaultsConfigSource — 默认配置源：装配上下文的不可变投影。

对应 defaults + 用户 YAML 合并后的扁平 key→str 表。改 YAML 不会自动反映到已创建
实例；要运行时改值请用 :class:`DictConfigSource` / :class:`OverlayConfigSource`。
"""

from __future__ import annotations

from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.config.project import project_assembly_values


class YamlDefaultsConfigSource(ConfigSource):
    """只读 YAML/defaults 投影。改 YAML 不会自动反映到已创建实例；要运行时改值请用 Dict/Overlay。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = {str(k): str(v) for k, v in (values or {}).items()}

    def fetch(self, key: str) -> str | None:
        """按稳定 key 取值；缺失返回 ``None``。"""
        if key not in self._values:
            return None
        return self._values[key]

    def health(self) -> None:
        """YAML/defaults 源无外部依赖，恒健康。"""
        return None

    def as_dict(self) -> dict[str, str]:
        """返回内部表的浅拷贝（只读调试用）。"""
        return dict(self._values)


@ConfigSourceProducer.register("yaml_defaults")
def _build_yaml_defaults(config) -> YamlDefaultsConfigSource:
    """从当前 AssemblyContext 投影；也允许 params.values 覆盖/补充。"""
    projected = project_assembly_values(config.ctx)
    extra = config.get("values") or {}
    if isinstance(extra, dict):
        projected.update({str(k): str(v) for k, v in extra.items()})
    return YamlDefaultsConfigSource(projected)
