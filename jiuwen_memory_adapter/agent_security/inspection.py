"""插件自有的检测协议；检测器可替换，不依赖 jiuwen memory API。"""

from dataclasses import dataclass, field
from typing import Protocol


class InspectionError(ValueError):
    """检测配置、输入或预算无效；调用方必须拒绝释放。"""


@dataclass(frozen=True)
class TextInspection:
    level_id: str
    redacted: str = field(repr=False)
    matched_rules: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


class TextInspector(Protocol):
    @property
    def version(self) -> str:
        """由显式注册的实现提供此端口。"""
        ...

    def inspect(self, text: str, *, label: str = "") -> TextInspection:
        """由显式注册的实现提供此端口。"""

        ...


def create_text_inspector(**configuration) -> TextInspector:
    """延迟创建默认正则实现；配置属于插件，与 SDK 写入/还原策略独立。"""
    from .detectors.regex import RegexTextInspector

    return RegexTextInspector.create(**configuration)
