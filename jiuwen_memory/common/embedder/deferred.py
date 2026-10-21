# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""可选向量化依赖的延迟代理，不提前校验或装配未使用的插件。"""

from collections.abc import Callable
from threading import RLock

from jiuwen_memory.common.base import PluginType

from .base import Embedder


class DeferredEmbedder(Embedder):
    """首次使用时解析一次；resolver 应由本轮 Producer.bind_named 绑定。"""

    def __init__(self, resolver: Callable[[], Embedder]) -> None:
        self._resolver = resolver
        self._delegate: Embedder | None = None
        self._lock = RLock()

    def _get(self) -> Embedder:
        with self._lock:
            if self._delegate is None:
                self._delegate = self._resolver()
            return self._delegate

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._get().embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._get().embed_query(text)

    def dimension(self) -> int:
        return self._get().dimension()

    def health(self) -> None:
        return self._get().health()

    def plugin_type(self) -> PluginType:
        return self._get().plugin_type()
