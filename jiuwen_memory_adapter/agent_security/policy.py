"""按边界/动作/目标配置的三级策略，认证授权检查独立于用户批准。"""

import hashlib
from dataclasses import dataclass, field
from types import MappingProxyType

from ._validation import has_exact_type
from .hooks import Hook
from .runtime import ApprovalChoice, DefaultEgressPolicy, SecurityDenied, encode_payload
from .sources import strongest

_EGRESS = frozenset(
    {
        Hook.BEFORE_LLM_CALL,
        Hook.BEFORE_TOOL_CALL,
        Hook.BEFORE_AGENT_RESPONSE,
        Hook.BEFORE_AGENT_MESSAGE,
    }
)


@dataclass(frozen=True)
class TargetRule:
    target: str
    hooks: frozenset[Hook] = _EGRESS
    actions: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    max_level: str = "S3"
    allow_original: bool = True

    def __post_init__(self):
        if not has_exact_type(self.target, str) or not self.target or len(self.target) > 256:
            raise SecurityDenied("目标策略标识无效")
        object.__setattr__(self, "hooks", frozenset(Hook(hook) for hook in self.hooks))
        object.__setattr__(self, "actions", frozenset(self.actions))
        if not self.hooks or self.hooks - _EGRESS or (not self.actions):
            raise SecurityDenied("目标策略边界或动作无效")
        elif (
            len(self.actions) > 1024
            or any(
                not has_exact_type(action, str) or not action or len(action) > 256
                for action in self.actions
            )
            or (not has_exact_type(self.allow_original, bool))
        ):
            raise SecurityDenied("目标策略边界或动作无效")
        strongest(self.max_level)


class EgressPolicy:
    def __init__(self, rules, *, version="policy-1"):
        if not has_exact_type(version, str) or not version or len(version) > 256:
            raise SecurityDenied("策略版本标识无效")
        rules = tuple(rules)
        if (
            not rules
            or len(rules) > 1024
            or any(not isinstance(rule, TargetRule) for rule in rules)
        ):
            raise SecurityDenied("目标规则重复、缺失或过多")
        elif len({r.target for r in rules}) != len(rules):
            raise SecurityDenied("目标规则重复、缺失或过多")
        self._rules = MappingProxyType({rule.target: rule for rule in rules})
        self._version_label = version
        self.allowed_targets = frozenset(self._rules)
        self.version = hashlib.sha256(encode_payload(self.to_mapping()).encode()).hexdigest()

    def to_mapping(self):
        """返回版本化规范策略；数据契约独立于具体框架和记忆后端。"""
        return {
            "schema_version": 1,
            "version": self._version_label,
            "targets": [
                {
                    "target": r.target,
                    "hooks": sorted(map(str, r.hooks)),
                    "actions": sorted(r.actions),
                    "max_level": r.max_level,
                    "allow_original": r.allow_original,
                }
                for r in sorted(self._rules.values(), key=lambda r: r.target)
            ],
        }

    @classmethod
    def from_mapping(cls, value):
        if (
            not has_exact_type(value, dict)
            or set(value) - {"schema_version", "version", "targets"}
            or (not has_exact_type(value.get("schema_version", 1), (int, float)))
        ):
            raise SecurityDenied("外发策略配置无效")
        elif value.get("schema_version", 1) != 1 or not has_exact_type(value.get("targets"), list):
            raise SecurityDenied("外发策略配置无效")
        try:
            encode_payload(value)
            for rule in value["targets"]:
                if not has_exact_type(rule, dict):
                    raise SecurityDenied("目标策略规则必须为对象")
                for key, limit in (("hooks", 4), ("actions", 1024)):
                    if key in rule:
                        if (
                            not has_exact_type(rule[key], list)
                            or not 0 < len(rule[key]) <= limit
                            or any(not has_exact_type(item, str) for item in rule[key])
                        ):
                            raise SecurityDenied("目标策略边界或动作列表无效")
                        if len(set(rule[key])) != len(rule[key]):
                            raise SecurityDenied("目标策略边界或动作列表无效")
            return cls(
                [TargetRule(**rule) for rule in value["targets"]],
                version=value.get("version", "policy-1"),
            )
        except (TypeError, ValueError, KeyError):
            raise SecurityDenied("外发策略规则无效") from None

    def permits(self, operation):
        rule = self._rules.get(operation.target)
        return bool(
            rule
            and operation.hook in rule.hooks
            and ("*" in rule.actions or operation.action in rule.actions)
            and strongest(operation.source_level, rule.max_level) == rule.max_level
        )

    def choices(self, operation, inspection):
        if not self.permits(operation):
            raise SecurityDenied("目标、动作、边界或来源等级超出授权")
        rule = self._rules[operation.target]
        if strongest(inspection.level, rule.max_level) != rule.max_level:
            raise SecurityDenied("载荷等级超出该目标策略")
        choices = DefaultEgressPolicy(self.allowed_targets).choices(operation, inspection)
        if not rule.allow_original:
            choices = tuple(c for c in choices if c != ApprovalChoice.CONTINUE_ORIGINAL)
        if choices == (ApprovalChoice.ABORT,):
            raise SecurityDenied("此目标没有允许的发送候选")
        return choices
