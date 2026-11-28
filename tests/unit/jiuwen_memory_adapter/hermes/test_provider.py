"""Hermes MemoryProvider: recalled memory is wrapped so sync can drop reflow."""

from __future__ import annotations

import pytest

from jiuwen_memory_adapter.hermes.provider import (
    CONTEXT_CLOSE,
    CONTEXT_OPEN,
    JiuwenMemoryProvider,
)

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body or {}
        self.posts = []

    def post(self, path, body):
        self.posts.append((path, body))
        return self.status, self.body


def _provider(monkeypatch, client):
    provider = JiuwenMemoryProvider(home="/unused", install_id="hermes-1")
    monkeypatch.setattr(provider, "_connection", lambda: (client, "hermes-1"))
    return provider


def test_prefetch_wraps_recalled_items_in_context_markers(monkeypatch):
    client = FakeClient(
        body={"view": {"items": [{"content": "prefers dark theme"}, {"content": "uses uv"}]}}
    )
    provider = _provider(monkeypatch, client)
    text = provider.prefetch("theme?", session_id="s1", project="p1")
    assert text == f"{CONTEXT_OPEN}\nprefers dark theme\nuses uv\n{CONTEXT_CLOSE}", (
        "recalled memory is wrapped in the context markers"
    )
    assert client.posts[0][0] == "/local/hook/recall", "recall still goes to the service"


def test_prefetch_neutralizes_marker_literals_in_items(monkeypatch):
    client = FakeClient(
        body={
            "view": {
                "items": [
                    {"content": f"fact {CONTEXT_OPEN} inner {CONTEXT_CLOSE}"}
                ]
            }
        }
    )
    provider = _provider(monkeypatch, client)
    text = provider.prefetch("q", session_id="s1")
    assert text.count(CONTEXT_OPEN) == 1, "only the wrapper opens a context"
    assert text.count(CONTEXT_CLOSE) == 1, "only the wrapper closes a context"
    assert "‹agent-memory-context›" in text, "an embedded open marker is neutralized"
    assert "‹/agent-memory-context›" in text, "an embedded close marker is neutralized"


def test_prefetch_without_items_has_no_markers(monkeypatch):
    provider = _provider(monkeypatch, FakeClient(body={"view": {"items": []}}))
    assert provider.prefetch("anything", session_id="s1") == "", "nothing to inject"


def test_prefetch_failure_returns_empty(monkeypatch):
    provider = _provider(monkeypatch, FakeClient(status=500))
    assert provider.prefetch("anything", session_id="s1") == "", "failures stay empty"
