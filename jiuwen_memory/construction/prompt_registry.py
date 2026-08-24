"""Prompt 注册表：从配置加载的命名 prompt 文本，供动态抽取/巩固/反思三步按 key 查询。

配置形态（yml 顶层 ``prompts`` 段）::

    prompts:
      extract: { episodic: "...", preference: "..." }
      consolidate: { episodic: "..." }
      reflect: { episodic: "..." }

metadata 只写 prompt 的 **key**；运行时由本注册表按 ``phase + key`` 取回真实文本。

引入 :class:`~config.config_source.ConfigSource` 后：优先 ``fetch("prompts.<phase>.<name>")``，
缺失再回退构造时的快照 dict（S08）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

PHASE_EXTRACT = "extract"
PHASE_CONSOLIDATE = "consolidate"
PHASE_REFLECT = "reflect"

if TYPE_CHECKING:
    from jiuwen_memory.config.config_source import ConfigSource


class PromptRegistry:
    """按阶段（extract/consolidate/reflect）和 key 查询 prompt 文本。

    缺 key 返回 ``None``，由调用方决定回退行为。
    """

    def __init__(
        self,
        prompts: Mapping[str, Mapping[str, str]] | None = None,
        *,
        config_source: ConfigSource | None = None,
    ) -> None:
        # 深拷贝避免外部改动影响注册表；value 统一转 str
        self._prompts: dict[str, dict[str, str]] = {}
        for phase, items in (prompts or {}).items():
            self._prompts[str(phase)] = {
                str(key): str(value) for key, value in items.items()
            }
        self._config_source = config_source

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any] | None,
        *,
        config_source: ConfigSource | None = None,
    ) -> "PromptRegistry":
        """从 yml 解析出的 ``prompts`` 段构造；可同时注入 ConfigSource 做运行时覆盖。"""
        if data is None or not isinstance(data, Mapping):
            return cls(config_source=config_source)
        return cls(dict(data), config_source=config_source)

    def get(self, phase: str, key: str) -> str | None:
        """按阶段和 key 取 prompt 文本；缺失返回 ``None``。

        若注入了 ConfigSource，优先读 ``prompts.<phase>.<key>``（支持运行时切换文本）。
        """
        if self._config_source is not None:
            from jiuwen_memory.config.keys import prompt_key

            live = self._config_source.fetch(prompt_key(phase, key))
            if live is not None:
                return live
        return self._prompts.get(phase, {}).get(key)

    def has_phase(self, phase: str) -> bool:
        """构造期快照是否包含该 phase（不含仅存在于 ConfigSource 的 live key）。"""
        return phase in self._prompts
