"""Normalized export contract; native product formats require real sample fixtures."""

import json

import pytest

from jiuwen_memory_adapter.continuity.history import import_history, prepare_history

pytestmark = pytest.mark.unit


def _prepare(path, batch="one", product="hermes", context="session"):
    return prepare_history(
        str(path), batch_id=batch, task_id="task", binding_id="binding",
        product=product, context_id=context, version="fixture",
    )


def test_native_event_identity_survives_batch_and_file_order(tmp_path):
    path = tmp_path / "history.jsonl"
    row = {"event_id": "native-1", "content": "evidence", "claimed_author": "author"}
    path.write_text(json.dumps(row))
    _, first = _prepare(path)
    path.write_text(json.dumps({"event_id": "native-2", "content": "other"})
                    + "\n" + json.dumps(row))
    _, second = _prepare(path, "two")
    assert first[0] == second[1]
    assert first[0]["claimed_author"] == "author"
    assert _prepare(path, product="workbuddy")[1][1]["event_id"] != first[0]["event_id"]
    assert _prepare(path, context="another")[1][1]["event_id"] != first[0]["event_id"]


def test_changed_content_preserves_id_for_server_conflict_detection(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps({"event_id": "1", "content": "before"}))
    _, first = _prepare(path)
    path.write_text(json.dumps({"event_id": "1", "content": "after"}))
    _, second = _prepare(path, "two")
    assert first[0]["event_id"] == second[0]["event_id"]
    assert first[0]["content"] != second[0]["content"]


@pytest.mark.parametrize("event_id", ["", " ", 42, [], {}])
def test_malformed_identity_rejected(tmp_path, event_id):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps({"event_id": event_id, "content": "text"}))
    with pytest.raises(ValueError, match="event_id"):
        _prepare(path)


@pytest.mark.parametrize("status", ["partial", "unknown", "rejected", "failed"])
def test_import_stops_at_every_uncommitted_boundary(status):
    class Client:
        @staticmethod
        def call(verb, payload):
            assert verb == "batch_add"
            assert payload["continue_on_error"] is False
            return {"outcomes": [{"continuity_status": status, "operation_id": "op"}]}

    events = [
        {"event_id": "1", "content": "a", "task_id": "t", "binding_id": "b"},
        {"event_id": "2", "content": "b", "task_id": "t", "binding_id": "b"},
    ]
    result = import_history(Client(), {}, {}, events)
    assert len(result["results"]) == 1
    # 停在第一个未提交项：该行与它后面的行都还没写入，`remaining` 按实际导入数算。
    assert result["imported"] == 0
    assert result["remaining"] == 2
