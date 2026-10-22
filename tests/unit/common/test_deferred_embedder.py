# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""可选 Embedder 首次使用前不构造，代理保留 query 专用路径和异常。"""

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder import DeferredEmbedder, Embedder
from jiuwen_memory.common.errors import ValidationError

pytestmark = pytest.mark.unit


class RecordingEmbedder(Embedder):
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(("embed", texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        self.calls.append(("embed_query", text))
        return [0.0, 1.0]

    def dimension(self):
        self.calls.append(("dimension",))
        return 2

    def health(self):
        self.calls.append(("health",))

    def plugin_type(self):
        self.calls.append(("plugin_type",))
        return PluginType.EMBEDDER


def test_deferred_embedder_preserves_all_operations_and_resolves_once():
    calls = []
    delegate = RecordingEmbedder()

    def resolve():
        calls.append("resolve")
        return delegate

    embedder = DeferredEmbedder(resolve)
    assert calls == []
    assert embedder.embed_query("question") == [0.0, 1.0]
    assert embedder.embed(["content"]) == [[1.0, 0.0]]
    assert embedder.dimension() == 2
    assert embedder.health() is None
    assert embedder.plugin_type() is PluginType.EMBEDDER
    assert calls == ["resolve"]
    assert delegate.calls == [
        ("embed_query", "question"), ("embed", ["content"]), ("dimension",),
        ("health",), ("plugin_type",),
    ]


def test_resolution_failure_is_not_hidden_or_cached():
    calls = []
    delegate = RecordingEmbedder()

    def resolve():
        calls.append("resolve")
        if len(calls) == 1:
            raise ValidationError("invalid optional model")
        return delegate

    embedder = DeferredEmbedder(resolve)
    with pytest.raises(ValidationError, match="invalid optional model"):
        embedder.dimension()
    assert embedder.dimension() == 2
    assert calls == ["resolve", "resolve"]
