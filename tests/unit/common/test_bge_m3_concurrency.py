"""BGEM3Embedder 的并发安全回归测试。

真实故障（R9000K 上 48 题并发评测崩溃）：

    NotImplementedError: Cannot copy out of meta tensor; no data!
      at FlagEmbedding/.../m3.py:346  →  self.model.to(device)

根因：``encode()`` 内部会按需 ``model.to(device)``。多个线程同时调用时，
第二个线程进入时权重已被前一个线程移走，只剩 meta 占位。

修复要求：
- 模型加载只执行一次（否则多线程各自构造一份，显存翻倍且互相覆盖）。
- 推理调用串行化（底层模型非线程安全）。
"""
from __future__ import annotations

import threading
import time

import numpy as np

from jiuwen_memory.common.embedder.embedder_impl.bge_m3_embedder import BGEM3Embedder


class _ConcurrencyDetectingModel:
    """模拟 FlagEmbedding 的非线程安全行为。

    若两个线程同时进入 encode，抛与真实故障同类的异常。
    """

    def __init__(self) -> None:
        self._active = 0
        self._lock = threading.Lock()
        self.max_concurrent = 0
        self.encode_calls = 0

    def encode(self, sentences, **_kwargs):
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            self.encode_calls += 1
            overlapping = self._active > 1
        try:
            time.sleep(0.01)  # 放大竞态窗口
            if overlapping:
                raise NotImplementedError(
                    "Cannot copy out of meta tensor; no data!"
                )
            return {"dense_vecs": np.array([[0.1] * 8 for _ in sentences])}
        finally:
            with self._lock:
                self._active -= 1


class _SlowLoadModel(_ConcurrencyDetectingModel):
    """加载阶段慢，暴露「多线程重复构造模型」的问题。"""

    constructions = 0
    _construction_lock = threading.Lock()

    def __init__(self) -> None:
        super().__init__()
        with _SlowLoadModel._construction_lock:
            _SlowLoadModel.constructions += 1
        time.sleep(0.02)


def _embedder_with_model(model) -> BGEM3Embedder:
    embedder = BGEM3Embedder(
        model_name_or_path="BAAI/bge-m3",
        use_fp16=False,
        dimension=8,
    )
    embedder._model = model  # 跳过真实加载
    return embedder


def test_concurrent_embed_does_not_enter_model_simultaneously() -> None:
    """并发调用 embed 时，底层模型不得被多个线程同时进入。"""
    model = _ConcurrencyDetectingModel()
    embedder = _embedder_with_model(model)
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            embedder.embed([f"text-{idx}"])
        except Exception as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"并发不应抛异常，实际: {errors}"
    assert model.max_concurrent == 1, (
        f"底层模型被 {model.max_concurrent} 个线程同时进入，未串行化"
    )


def test_concurrent_calls_all_return_results_in_order() -> None:
    """并发调用的返回值必须完整且与输入一一对应。"""
    embedder = _embedder_with_model(_ConcurrencyDetectingModel())
    results: dict[int, list[list[float]]] = {}

    def worker(idx: int) -> None:
        results[idx] = embedder.embed([f"a-{idx}", f"b-{idx}"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    for idx, vectors in results.items():
        assert len(vectors) == 2, f"第 {idx} 次调用返回条数不对"


def test_lazy_load_happens_exactly_once_under_concurrency() -> None:
    """模型加载必须只发生一次——否则多线程各建一份，显存翻倍。"""
    _SlowLoadModel.constructions = 0
    embedder = BGEM3Embedder(
        model_name_or_path="BAAI/bge-m3",
        use_fp16=False,
        dimension=8,
    )
    built = _SlowLoadModel()

    def fake_load():
        if embedder._model is not None:
            return
        time.sleep(0.02)
        embedder._model = built

    embedder._load_model = fake_load  # type: ignore[method-assign]

    def worker() -> None:
        embedder.embed(["x"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert embedder._model is built
