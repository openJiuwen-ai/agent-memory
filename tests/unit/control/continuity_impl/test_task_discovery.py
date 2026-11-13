"""Task discovery, hierarchy and continuity background selection over a real Registry."""

from dataclasses import asdict
from math import log1p

import pytest

from jiuwen_memory.api import MemoryRef, Scope, TaskRequest, ValidationError
from jiuwen_memory.common.type_def.continuity import RoleViewQuery, fingerprint
from jiuwen_memory.control.continuity_impl.task_search import TaskSearchIndex

pytestmark = pytest.mark.unit


class SemanticEmbedder:
    """Deterministic semantic fixture: different words can express the same topic."""

    @staticmethod
    def embed(texts):
        return [[float(any(word in text.lower() for word in ("memory", "recall"))),
                 float("payment" in text.lower())] for text in texts]

    def embed_query(self, text):
        return self.embed([text])[0]


def test_find_prefers_context_then_semantic_and_scope(setup):
    service, scope, _, read, _, _ = setup
    service.task_search = TaskSearchIndex(SemanticEmbedder())
    memory = service.task(scope, TaskRequest("create", "m", title="memory report"), "o", read)
    payment = service.task(scope, TaskRequest("create", "p", title="payment report"), "o", read)
    service.task(scope, TaskRequest("bind", "b", payment["task_id"], 1,
                                   connector_id="host", context_id="A"), "o", read)
    result = service.task(scope, TaskRequest("find", connector_id="host", context_id="A",
                                            query="recall research"), "o", read)
    assert [item["task_id"] for item in result["tasks"]] == [payment["task_id"]]
    assert result["tasks"][0]["match_type"] == "context_exact"
    for request in (TaskRequest("find", connector_id="host", context_id="B", query="recall"),
                    TaskRequest("find", query="recall")):
        result = service.task(scope, request, "o", read)
        assert [item["task_id"] for item in result["tasks"]] == [memory["task_id"]]
        assert result["tasks"][0]["match_type"] == "description_semantic"
    other = Scope(org=scope.org, space="other")
    assert service.task(other, TaskRequest("find", query="recall"), "o", read)["tasks"] == []
    assert service.task(scope, TaskRequest("find", connector_id="host", context_id="B"),
                        "o", read)["tasks"] == []


def test_find_without_embedder_prefers_context_then_uses_bm25(setup):
    service, scope, _, read, _, _ = setup
    task = service.task(scope, TaskRequest("create", "memory", title="memory report"), "o", read)
    service.task(scope, TaskRequest("bind", "bind", task["task_id"], 1,
                                   connector_id="host", context_id="A"), "o", read)
    exact = service.task(scope, TaskRequest("find", connector_id="host", context_id="A",
                                          query="recall"), "o", read)
    assert [item["task_id"] for item in exact["tasks"]] == [task["task_id"]]
    result = service.task(scope, TaskRequest("find", query="memory"), "o", read)
    assert [item["task_id"] for item in result["tasks"]] == [task["task_id"]]
    assert result["tasks"][0]["match_type"] == "description_bm25"
    assert result["tasks"][0]["score"] > 0
    other = Scope(org=scope.org, space="other")
    assert service.task(other, TaskRequest("find", query="memory"), "o", read)["tasks"] == []


def test_bm25_ranking_top_k_and_registry_changes(setup):
    service, scope, _, read, _, _ = setup
    first = service.task(scope, TaskRequest("create", "first", title="memory report"), "o", read)
    second = service.task(scope, TaskRequest("create", "second", title="payment report"), "o", read)
    request = TaskRequest("find", query="memory report", top_k=1)
    result = service.task(scope, request, "o", read)
    assert [item["task_id"] for item in result["tasks"]] == [first["task_id"]]
    service.task(scope, TaskRequest("close", "close", first["task_id"], 1), "o", read)
    result = service.task(scope, request, "o", read)
    assert [item["task_id"] for item in result["tasks"]] == [second["task_id"]]
    for query in ("nonexistent", "!?", "memory"):
        assert service.task(scope, TaskRequest("find", query=query), "o", read)["tasks"] == []


def test_bm25_chinese_description_and_stable_tie_order(setup):
    service, scope, _, read, _, _ = setup
    tasks = []
    for request_id in ("a", "b"):
        tasks.append(service.task(scope, TaskRequest("create", request_id,
                                  title="报告", description="整理跨会话任务接续"), "o", read))
    result = service.task(scope, TaskRequest("find", query="任务接续"), "o", read)
    assert [item["task_id"] for item in result["tasks"]] == sorted(
        task["task_id"] for task in tasks
    )
    assert all(item["match_type"] == "description_bm25" for item in result["tasks"])


def test_bm25_uses_term_frequency_and_document_length(setup):
    service, scope, _, read, _, _ = setup
    tasks = []
    for request_id, title in (("short", "memory"), ("long", "memory other other other"),
                              ("repeated", "memory memory")):
        tasks.append(service.task(scope, TaskRequest("create", request_id, title=title), "o", read))
    result = service.task(scope, TaskRequest("find", query="memory"), "o", read)
    assert [item["task_id"] for item in result["tasks"]] == [
        tasks[2]["task_id"], tasks[0]["task_id"], tasks[1]["task_id"],
    ]
    expected = log1p(0.5 / 3.5) * 2 * 2.2 / (2 + 1.2 * (0.25 + 0.75 * 2 / (7 / 3)))
    assert result["tasks"][0]["score"] == pytest.approx(expected)


def test_configured_embedder_failure_does_not_fall_back_to_bm25(setup):
    class BrokenEmbedder:
        @staticmethod
        def embed(texts):
            raise RuntimeError("embedding unavailable")

    service, scope, _, read, _, _ = setup
    service.task_search = TaskSearchIndex(BrokenEmbedder())
    service.task(scope, TaskRequest("create", "memory", title="memory report"), "o", read)
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        service.task(scope, TaskRequest("find", query="memory"), "o", read)


@pytest.mark.parametrize("query", [None, "recall"])
def test_find_skips_closed_context_task_and_allows_semantic_fallback(setup, query):
    service, scope, _, read, _, _ = setup
    service.task_search = TaskSearchIndex(SemanticEmbedder())
    closed = service.task(scope, TaskRequest("create", "closed", title="memory report"), "o", read)
    service.task(scope, TaskRequest("bind", "bind", closed["task_id"], 1,
                                   connector_id="host", context_id="A"), "o", read)
    service.task(scope, TaskRequest("close", "close", closed["task_id"], 2), "o", read)
    opened = service.task(scope, TaskRequest("create", "open", title="memory report"), "o", read)

    result = service.task(scope, TaskRequest("find", connector_id="host", context_id="A",
                                           query=query), "o", read)
    assert [item["task_id"] for item in result["tasks"]] == (
        [opened["task_id"]] if query else []
    )
    if query:
        assert result["tasks"][0]["match_type"] == "description_semantic"
    # Closing preserves the record and its binding for inspection, but not discovery.
    stored = service.task(scope, TaskRequest("get", task_id=closed["task_id"]), "o", read)
    assert stored["state"] == "closed"
    assert stored["bindings"][0]["active"]


def test_hierarchy_and_legacy_description_backfill(setup):
    service, scope, _, read, _, _ = setup
    root = service.task(scope, TaskRequest("create", "root"), "o", read)
    child = service.task(scope, TaskRequest("create", "child", parent_task_id=root["task_id"],
                                          title="part one"), "o", read)
    grandchild = service.task(scope, TaskRequest("create", "grandchild",
                                               parent_task_id=child["task_id"]), "o", read)
    assert root["task"]["parent_task_id"] is None
    assert root["task"]["root_task_id"] == root["task_id"]
    assert grandchild["task"]["root_task_id"] == root["task_id"]
    bound = service.task(scope, TaskRequest("bind", "bind", root["task_id"], 1,
                                           connector_id="host", context_id="A",
                                           title="memory report"), "o", read)
    assert bound["task"]["title"] == "memory report"
    assert service.task(scope, TaskRequest("bind", "bind", root["task_id"], 1,
                                         connector_id="host", context_id="A",
                                         title="memory report"), "o", read)["replayed"]


def test_pre_extension_receipt_remains_replayable(setup):
    service, scope, _, read, _, _ = setup
    request = TaskRequest("create", "old-request")
    task_id = fingerprint({"owner": "o", "request_id": request.request_id})
    payload = asdict(request)
    for key in ("title", "description", "parent_task_id", "query", "top_k"):
        payload.pop(key)
    service.repository.mutate(scope, "tasks", task_id, 0, request.request_id, payload,
                              lambda task: None, initial={"task_id": task_id, "owner": "o",
                              "revision": 0, "state": "open", "bindings": [], "background": []})
    result = service.task(scope, request, "o", read)
    assert result["replayed"]
    assert result["task"]["root_task_id"] == task_id


def test_continuity_required_then_latest_with_original_budget_gap(setup):
    service, scope, write, read, _, _ = setup
    service.mode = "continuity"
    old, required, latest = [write(text)[0] for text in ("old", "required", "latest")]
    task = service.task(scope, TaskRequest("create", "create"), "o", read)
    for n, (unit, needed) in enumerate(((old, False), (required, True), (latest, False)), 1):
        task = service.task(scope, TaskRequest("attach", str(n), task["task_id"], n,
                            background=[MemoryRef.of(unit)], required=needed), "o", read)
    view = service.continuity_view_result(scope, RoleViewQuery(task_id=task["task_id"]), read,
                                          extra_items=[], top_k=2)
    assert [entry["text"] for entry in view["items"]] == ["required", "latest"]
    assert any(gap["code"] == "budget_exhausted" and not gap["blocking"]
               for gap in view["gaps"])


@pytest.mark.parametrize("arguments", [{"connector_id": "host"}, {"context_id": "A"},
                                      {"top_k": 0}, {"query": 5}])
def test_invalid_find_request(arguments):
    with pytest.raises(ValidationError):
        TaskRequest("find", **arguments)
