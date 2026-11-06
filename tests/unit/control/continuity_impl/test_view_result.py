"""角色档视图归一化：按发现来源排序后再截断，必需背景不被关系/语义条目挤出。"""

import pytest

pytestmark = pytest.mark.unit


def _entry(unit_id: str, kind: str, discovery: str, required: bool = False) -> dict:
    return {
        "id": f"m-{unit_id}",
        "text": unit_id,
        "kind": kind,
        "status": "reported",
        "discovery": discovery,
        "required": required,
        "classification_confidence": "unknown",
        "evidence_refs": [{"scope": {}, "unit_id": unit_id, "content_digest": "0" * 64}],
        "conditions": [],
        "duty_ids": [],
        "valid_duties": [],
        "common": discovery != "relation",
        "path_ids": ["p1"] if discovery == "relation" else [],
    }


def test_truncation_keeps_required_background_even_in_a_later_group(setup, monkeypatch):
    service, scope, _write, read, _denied, _units = setup
    view = {
        "schema_version": "role-view/2",
        "anchors": [],
        # 分组顺序 facts → judgments：必需背景放在后面的分组里，按分组截断就会被挤掉。
        "facts": [
            _entry("semantic", "statement", "semantic_candidate"),
            _entry("related", "observation", "relation"),
        ],
        "judgments": [_entry("background", "inference", "background", required=True)],
        "experiences": [],
        "open_issues": [],
        "risks": [],
        "paths": [{"path_id": "p1"}],
        "gaps": [],
        "meta": {},
    }
    monkeypatch.setattr(service, "view", lambda *args, **kwargs: view)

    result = service.view_result(scope, None, read, None, top_k=2)

    assert [item["unit_id"] for item in result["items"]] == ["background", "related"]
    assert result["items"][0]["annotation"]["required"] is True
    assert "id" not in result["items"][0]["annotation"], "entry id must not leak into annotation"
    assert "text" not in result["items"][0]["annotation"]
    assert any(gap["code"] == "budget_exhausted" for gap in result["gaps"]), result["gaps"]
    assert result["meta"]["mode"] == "role"
