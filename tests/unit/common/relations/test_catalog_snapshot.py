"""Effective relation catalog projection used by runtime inspectors."""

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.relations.catalog import RelationCatalog

pytestmark = pytest.mark.unit


def test_snapshot_preserves_effective_metadata_rules_and_budgets():
    catalog = RelationCatalog(
        {
            "supports": {"display_name": "支持依据", "description": "configured support"},
            "mitigates": {
                "display_name": "缓解",
                "description": "A mitigates B",
                "valid_source_kinds": ["statement", "experience"],
                "valid_target_kinds": ["statement", "observation"],
            },
        },
        {
            "security": {
                "display_name": "安全审查",
                "description": "检查风险缓解与验证依据",
                "traversal": [
                    {
                        "edge_type": "mitigates",
                        "direction": "backward",
                        "priority": 1,
                        "max_depth": 2,
                    }
                ],
                "budget": {"max_depth": 2, "max_nodes": 30},
            }
        },
    )

    snapshot = catalog.snapshot()
    duties = {item["id"]: item for item in snapshot["duties"]}
    relations = {item["id"]: item for item in snapshot["relation_types"]}

    assert snapshot["policy_version"] == catalog.version
    assert snapshot["default_duty"] == "general"
    assert duties["security"]["display_name"] == "安全审查"
    assert duties["security"]["description"] == "检查风险缓解与验证依据"
    assert duties["security"]["budget"]["max_nodes"] == 30
    assert {(rule["edge_type"], rule["direction"]) for rule in duties["security"]["traversal"]} >= {
        ("mitigates", "backward"),
        ("contradicts", "both"),
        ("requires", "forward"),
    }
    assert relations["supports"]["display_name"] == "支持依据"
    assert relations["mitigates"]["valid_source_kinds"] == ["statement", "experience"]

    duties["security"]["display_name"] = "mutated"
    assert catalog.snapshot()["duties"][-1]["display_name"] != "mutated"


@pytest.mark.parametrize(
    "policy",
    [
        {"display_name": 1},
        {"description": []},
        {"traversal": "bad"},
        {"unknown": True},
    ],
)
def test_snapshot_rejects_invalid_duty_metadata(policy):
    with pytest.raises(ValidationError):
        RelationCatalog(duty_policies={"security": policy})
