"""最小实现：:class:`~control.policy.PolicyManager`。

内存策略表：仅允许调整已知键；未知键（含试图新增）抛
:class:`~common.errors.PolicyError`。
"""

from __future__ import annotations

from typing import Dict

from jiuwen_memory.common.errors import PolicyError
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.policy import PolicyManager, PolicyProducer


class DictPolicyManager(PolicyManager):
    """内存策略表：仅允许改已知键，未知键抛 :class:`PolicyError`。"""

    def __init__(self, policies: Dict[str, str] | None = None) -> None:
        """初始化 DictPolicyManager。

        Args:
            policies: 参数 policies（Dict[str, str] | None）。
        """
        self._policies: Dict[str, str] = dict(policies or {})

    def operator_type(self) -> ControlOperatorType:
        """返回当前算子类型。

        Returns:
            返回 ControlOperatorType。
        """
        return ControlOperatorType.POLICY

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def get(self, key: str) -> str:
        """读取指定的记录或资源。

        Args:
            key: 参数 key（str）。

        Returns:
            返回 str。

        Raises:
            PolicyError: 执行失败时抛出。
        """
        if key not in self._policies:
            raise PolicyError(f"unknown policy key: {key!r}")
        return self._policies[key]

    def set(self, key: str, value: str) -> None:
        """执行 `set` 操作。

        Args:
            key: 参数 key（str）。
            value: 参数 value（str）。

        Raises:
            PolicyError: 执行失败时抛出。
        """
        if key not in self._policies:
            raise PolicyError(f"unknown policy key: {key!r}")
        self._policies[key] = value

    def all(self) -> Dict[str, str]:
        """执行 `all` 操作。

        Returns:
            返回 Dict[str, str]。
        """
        return dict(self._policies)


# -- 注册到 PolicyProducer（实现自注册，新增无需改 producer/build_kernel） -------- #

# 缺省策略：rerank 开 + lifecycle sweep 目标态。后两键由 KVLifecycleManager 读取，
# 缺失会令 sweep 在 policy.get 处抛 PolicyError，故必须随缺省一并装上。
_DEFAULT_POLICIES = {
    "rerank.enabled": "true",
    "lifecycle.expired_active.target": "forgotten",
    "lifecycle.superseded.target": "forgotten",
    "scope.require_space": "false",
}


@PolicyProducer.register("dict")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return DictPolicyManager(dict(config.get("policies", _DEFAULT_POLICIES)))
