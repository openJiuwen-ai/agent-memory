"""只读取宿主已经解析的 agent_security 配置段，不引入另一套配置加载器。"""

from collections.abc import Mapping
from dataclasses import dataclass

from ._validation import has_exact_type


class ConfigurationError(ValueError):
    """配置、驱动或公开接口声明无效。"""


@dataclass(frozen=True)
class AgentSecurityConfig:
    framework: str
    memory_backend: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.framework, str) or not self.framework.strip():
            raise ConfigurationError("agent_security.framework 必须是非空字符串")
        if self.framework != self.framework.strip():
            raise ConfigurationError("agent_security.framework 不允许首尾空白")
        if self.memory_backend is not None:
            if (
                not has_exact_type(self.memory_backend, str)
                or not self.memory_backend
                or self.memory_backend.strip() != self.memory_backend
            ):
                raise ConfigurationError("memory_backend 必须为后端标识或省略")

    @classmethod
    def from_mapping(cls, section: Mapping[str, object]) -> "AgentSecurityConfig":
        if not isinstance(section, Mapping):
            raise ConfigurationError("agent_security 必须是配置对象")
        if "framework" not in section or set(section) - {"framework", "memory_backend"}:
            raise ConfigurationError("agent_security 接受 framework 与可选 memory_backend")
        framework = section["framework"]
        if not isinstance(framework, str):
            raise ConfigurationError("agent_security.framework 必须是非空字符串")
        return cls(framework=framework, memory_backend=section.get("memory_backend"))
