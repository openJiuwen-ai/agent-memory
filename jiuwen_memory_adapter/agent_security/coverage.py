"""只报告实际装配的路径；映射描述与运行期覆盖报告分离。"""

from dataclasses import dataclass

from .hooks import Hook
from .runtime import SecurityDenied


@dataclass(frozen=True)
class CoverageReport:
    framework: str
    protected: frozenset[Hook]
    disabled: tuple[str, ...]
    tools: tuple[str, ...]
    targets: tuple[str, ...]
    protocol: str = "text-chat"
    memory_backends: tuple[str, ...] = ()
    memory_operations: tuple[str, ...] = ()

    def public(self):
        return {
            "framework": self.framework,
            "protocol": self.protocol,
            "protected": sorted(map(str, self.protected)),
            "disabled": list(self.disabled),
            "tools": list(self.tools),
            "targets": list(self.targets),
            "scope": "managed-agent-paths",
            "persistent_approval": False,
            "memory_backends": list(self.memory_backends),
            "memory_operations": list(self.memory_operations),
        }

    def require(self, hooks):
        if frozenset(Hook(h) for h in hooks) - self.protected:
            raise SecurityDenied("请求的安全边界未实际接线")
