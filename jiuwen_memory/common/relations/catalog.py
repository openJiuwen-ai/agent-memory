"""Startup-validated relation semantics and duty policies; no identity or storage logic."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def.continuity import ViewBudget, fingerprint

KINDS = frozenset(
    {
        "observation",
        "statement",
        "judgment",
        "result",
        "experience",
        "correction",
        "unclassified",
        "method",
        "plan",
        "action",
    }
)


@dataclass(frozen=True)
class RelationTypeSpec:
    name: str
    display_name: str
    description: str
    direction: str = "forward"
    symmetric: bool = False
    transitive: bool = False
    valid_source_kinds: tuple[str, ...] = tuple(sorted(KINDS))
    valid_target_kinds: tuple[str, ...] = tuple(sorted(KINDS))

    def __post_init__(self):
        if not self.name or self.direction not in {"forward", "backward", "both"}:
            raise ValidationError("invalid relation name or direction")
        if self.symmetric != (self.direction == "both"):
            raise ValidationError("symmetric relation must use both direction")
        for attr in ("valid_source_kinds", "valid_target_kinds"):
            kinds = tuple(getattr(self, attr))
            if not kinds or not set(kinds) <= KINDS:
                raise ValidationError("unknown or empty endpoint kinds")
            object.__setattr__(self, attr, kinds)


@dataclass(frozen=True)
class TraversalRule:
    edge_type: str
    direction: str = "both"
    priority: int = 1
    max_depth: int = 2

    def __post_init__(self):
        if self.direction not in {"forward", "backward", "both"}:
            raise ValidationError("invalid traversal direction")
        if type(self.priority) is not int or self.priority < 0:
            raise ValidationError("invalid traversal priority")
        if type(self.max_depth) is not int or not 1 <= self.max_depth <= 4:
            raise ValidationError("invalid traversal depth")

    def matches(self, relation: str, direction: str, depth: int) -> bool:
        return (
            relation == self.edge_type
            and depth < self.max_depth
            and (
                self.direction == "both"
                or self.direction == {"out": "forward", "in": "backward"}[direction]
            )
        )


class RelationCatalog:
    """Immutable configured data, shared by construction and retrieval in one assembly."""

    def __init__(self, relation_types: dict | None = None, duty_policies: dict | None = None):
        descriptions = {
            "supports": "Evidence A supports judgment B; similarity is not support.",
            "contradicts": "A and B are incompatible in the same conditions; symmetric.",
            "possible_cause_of": "A is a possible cause of B; not proven causality.",
            "verification_method_for": "A describes how to test B, not a completed test.",
            "verifies": "Result A verifies B only under explicit conditions.",
            "requires": "Action A requires B; does not assert B is satisfied.",
        }
        types = {
            name: RelationTypeSpec(
                name,
                name,
                desc,
                direction="both" if name == "contradicts" else "forward",
                symmetric=name == "contradicts",
                transitive=name == "requires",
            )
            for name, desc in descriptions.items()
        }
        for name, raw in (relation_types or {}).items():
            data = deepcopy(raw)
            if name in types:
                if set(data) - {"display_name", "description"}:
                    raise ValidationError("built-in relation semantics cannot be overridden")
                from dataclasses import asdict

                data = {**asdict(types[name]), **data}
            else:
                data["name"] = name
            types[name] = RelationTypeSpec(**data)
        self.types = MappingProxyType(types)
        defaults = {
            "general": [(name, "both") for name in types],
            "diagnosis": [("possible_cause_of", "both"), ("supports", "backward")],
            "testing": [("verification_method_for", "backward"), ("verifies", "backward")],
            "execution": [("requires", "forward"), ("verifies", "backward")],
        }
        policies = {
            name: {"traversal": [{"edge_type": t, "direction": d} for t, d in rules]}
            for name, rules in defaults.items()
        }
        policies.update(deepcopy(duty_policies or {}))
        rules, budgets = {}, {}
        for name, policy in policies.items():
            if not name:
                raise ValidationError("empty duty name")
            entries = [TraversalRule(**r) for r in policy["traversal"]]
            if any(r.edge_type not in types for r in entries):
                raise ValidationError("duty references unknown relation type")
            # Mandatory checks cannot be removed or weakened by user policy.
            entries += [
                TraversalRule("contradicts", "both", 0, 4),
                TraversalRule("requires", "forward", 0, 4),
            ]
            rules[name] = tuple(sorted(entries, key=lambda r: r.priority))
            budgets[name] = MappingProxyType(vars(ViewBudget(**policy.get("budget", {}))))
        self.rules = MappingProxyType(rules)
        self.budgets = MappingProxyType(budgets)
        self.version = fingerprint({"relations": relation_types or {}, "duties": policies})

    def policy(self, duty: str):
        try:
            return self.rules[duty]
        except KeyError:
            raise ValidationError("unknown duty") from None

    def validate(self, relation, source, target):
        try:
            spec = self.types[relation.relation_type]
        except KeyError:
            raise ValidationError("unknown relation type") from None
        for unit, allowed in ((source, spec.valid_source_kinds), (target, spec.valid_target_kinds)):
            kind = unit.system_metadata.get("continuity_kind", "statement")
            if kind not in allowed:
                raise ValidationError("relation endpoint kind is not permitted")

    def effective_budget(self, duty: str, requested: ViewBudget) -> ViewBudget:
        self.policy(duty)
        return ViewBudget(**{k: min(v, self.budgets[duty][k]) for k, v in vars(requested).items()})
