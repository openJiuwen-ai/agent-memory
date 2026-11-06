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
        def call(*args):
            return {"status": status}

    result = import_history(Client(), {}, {}, [{"event_id": "1"}, {"event_id": "2"}])
    assert len(result["results"]) == 1
    assert result["remaining"] == 1
