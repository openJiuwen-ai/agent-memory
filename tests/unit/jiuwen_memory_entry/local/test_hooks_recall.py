"""Recall decisions: project, binding, pending record and recall budget (F17 D8)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory_entry.local.hooks import HookService, InstallPolicy, RecallRequest
from jiuwen_memory_entry.local.turns import Selection, TurnKey, TurnStore

pytestmark = pytest.mark.unit


class Item:
    def __init__(self, unit_id, content):
        self.unit_id = unit_id
        self.content = content


class View:
    def __init__(self, items, paths=None, gaps=None, view_meta=None):
        self.items = items
        self.paths = paths or []
        self.gaps = gaps or []
        self.view_meta = view_meta or {"mode": "continuity"}


class FakeApi:
    def __init__(self, tasks=None, bindings=None, view=None, description=None):
        self.tasks = tasks or []
        self.bindings = bindings or []
        self.view = view or View([Item("u1", "hello")])
        self.description = description or []
        self.searches = []

    def continuity_task(self, scope, request, *, security):
        if request.operation == "find":
            if request.connector_id:
                return {"tasks": list(self.tasks)}
            return {"tasks": list(self.description)}
        if request.operation == "get":
            return {"task_id": request.task_id, "bindings": list(self.bindings)}
        raise AssertionError(request.operation)

    def search(self, query, context, *, security, top_k=10, task_id=None, duty=None):
        self.searches.append(
            {"query": query, "task_id": task_id, "duty": duty, "top_k": top_k,
             "space": context.scope.space}
        )
        return self.view


def _service(tmp_path, api, *, capture="task", project="p1", projects=("p1",)):
    store = TurnStore(tmp_path / "turns.db")
    policy = InstallPolicy(
        install_id="codex-1",
        connector_id="codex-1",
        capture=capture,
        fixed_project=project if len(projects) == 1 else None,
        projects=tuple(projects),
        org="local",
    )
    return HookService(api, store, policy), store


def _request(**overrides):
    values = {"host_session_id": "s1", "turn_id": "t1", "prompt": "how do I fix it"}
    values.update(overrides)
    return RecallRequest(**values)


def test_single_binding_recalls_and_creates_pending(tmp_path):
    api = FakeApi(tasks=[{"task_id": "task-a", "match_type": "context_exact"}],
                  bindings=[{"binding_id": "b1", "connector_id": "codex-1",
                             "context_id": "s1", "active": True}])
    service, store = _service(tmp_path, api)
    result = service.recall(None, _request())
    assert result.status == "ok"
    assert result.view["items"][0]["content"] == "hello"
    assert api.searches[0]["task_id"] == "task-a"
    pending = store.get_pending(TurnKey("codex-1", "p1", "s1", "t1"))
    assert pending is not None and pending.user_text == "how do I fix it"


def test_capture_off_does_not_store_user_text(tmp_path):
    api = FakeApi(tasks=[{"task_id": "task-a", "match_type": "context_exact"}],
                  bindings=[{"binding_id": "b1", "connector_id": "codex-1",
                             "context_id": "s1", "active": True}])
    service, store = _service(tmp_path, api, capture="off")
    service.recall(None, _request())
    assert store.get_pending(TurnKey("codex-1", "p1", "s1", "t1")) is None


def test_unbound_shows_notice_and_candidates_without_pending(tmp_path):
    api = FakeApi(description=[{"task_id": "task-z", "title": "other"}])
    service, store = _service(tmp_path, api)
    result = service.recall(None, _request())
    assert result.status == "unbound"
    assert "will not be saved" in result.notice
    assert result.candidates[0]["task_id"] == "task-z"
    assert store.get_pending(TurnKey("codex-1", "p1", "s1", "t1")) is None


def test_multiple_bindings_require_selection(tmp_path):
    api = FakeApi(
        tasks=[{"task_id": "a"}, {"task_id": "b"}],
        bindings=[],
    )
    api.bindings = [
        {"binding_id": "b1", "connector_id": "codex-1", "context_id": "s1", "active": True},
        {"binding_id": "b2", "connector_id": "codex-1", "context_id": "s1", "active": True},
    ]

    def get(scope, request, *, security):
        return {"bindings": api.bindings}

    def combined(scope, request, *, security):
        if request.operation == "find":
            return {"tasks": api.tasks}
        return get(scope, request, security=security)

    api.continuity_task = combined
    service, _ = _service(tmp_path, api)
    result = service.recall(None, _request())
    assert result.status == "need_selection"
    assert len(result.candidates) == 2


def test_explicit_selection_is_saved_and_used(tmp_path):
    api = FakeApi(tasks=[{"task_id": "a"}, {"task_id": "b"}])
    api.bindings = [
        {"binding_id": "b1", "connector_id": "codex-1", "context_id": "s1", "active": True},
        {"binding_id": "b2", "connector_id": "codex-1", "context_id": "s1", "active": True},
    ]

    def find(scope, request, *, security):
        return {"tasks": [{"task_id": "a", "binding_id": "b1"},
                          {"task_id": "b", "binding_id": "b2"}]}

    def combined(scope, request, *, security):
        if request.operation == "find":
            return find(scope, request, security=security)
        binding = "b1" if request.task_id == "a" else "b2"
        return {
            "bindings": [
                {"binding_id": binding, "connector_id": "codex-1",
                 "context_id": "s1", "active": True}
            ]
        }

    api.continuity_task = combined
    service, store = _service(tmp_path, api)
    selection = Selection("codex-1", "p1", "s1", "b", "b2")
    result = service.recall(None, _request(selection=selection))
    assert result.status == "ok"
    assert api.searches[0]["task_id"] == "b"
    assert store.get_selection("codex-1", "p1", "s1").task_id == "b"


def test_multiple_projects_need_explicit_project(tmp_path):
    api = FakeApi()
    service, _ = _service(tmp_path, api, projects=("p1", "p2"))
    result = service.recall(None, _request())
    assert result.status == "need_project"
    with pytest.raises(ValidationError):
        service.recall(None, _request(project="other"))


def test_duty_and_top_k_are_forwarded(tmp_path):
    api = FakeApi(tasks=[{"task_id": "t"}],
                  bindings=[{"binding_id": "b", "connector_id": "codex-1",
                             "context_id": "s1", "active": True}])
    service, _ = _service(tmp_path, api)
    service.recall(None, _request(duty="diagnosis", top_k=3))
    call = api.searches[0]
    assert call["duty"] == "diagnosis" and call["top_k"] == 3 and call["space"] == "p1"


def test_view_is_trimmed_to_byte_budget(tmp_path):
    big = View([Item(f"u{i}", "x" * 4000) for i in range(20)])
    api = FakeApi(tasks=[{"task_id": "t"}],
                  bindings=[{"binding_id": "b", "connector_id": "codex-1",
                             "context_id": "s1", "active": True}],
                  view=big)
    service, _ = _service(tmp_path, api)
    result = service.recall(None, _request())
    import json

    assert len(json.dumps(result.view, ensure_ascii=False).encode()) <= 16 * 1024
    assert any(gap.get("code") == "budget_exhausted" for gap in result.view["gaps"])


def test_slow_search_does_not_block_past_the_budget(tmp_path):
    import time

    class SlowApi(FakeApi):
        def search(self, *args, **kwargs):
            time.sleep(0.5)
            return super().search(*args, **kwargs)

    api = SlowApi(tasks=[{"task_id": "t"}],
                  bindings=[{"binding_id": "b", "connector_id": "codex-1",
                             "context_id": "s1", "active": True}])
    service, _ = _service(tmp_path, api)
    service._timeout = 0.05
    started = time.monotonic()
    result = service.recall(None, _request())
    elapsed = time.monotonic() - started
    assert result.view is None
    assert elapsed < 0.4, "recall waited for the slow search to finish"


def test_recall_executor_bounds_slow_workers_and_drains():
    import threading

    from jiuwen_memory_entry.local.hooks import RecallExecutor

    executor = RecallExecutor(max_workers=1)
    gate = threading.Event()
    assert executor.run(lambda: gate.wait(1), 0.01) is None
    assert executor.run(lambda: "second", 0.05) is None, "no second slot while slow"
    gate.set()
    executor.close()
