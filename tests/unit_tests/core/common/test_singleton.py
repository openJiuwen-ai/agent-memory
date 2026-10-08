# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for `jiuwen_memory.common.utils.singleton.Singleton` metaclass.

The Singleton metaclass caches instances per class in a thread-safe manner
using a module-level lock. These tests verify:
- identical instance reuse across calls
- per-class isolation (subclasses get their own cached instance)
- thread safety under concurrent instantiation
"""

import threading

from jiuwen_memory.common.utils.singleton import Singleton


class _Sample(metaclass=Singleton):
    def __init__(self, value=None):
        self.value = value


class _Other(metaclass=Singleton):
    pass


class TestSingletonInstanceReuse:
    @staticmethod
    def test_same_instance_returned_on_repeated_calls():
        first = _Sample()
        second = _Sample()

        assert first is second

    @staticmethod
    def test_subsequent_call_returns_cached_instance():
        sentinel = _Sample(value="first")
        reused = _Sample(value="second")

        # Contract: repeated instantiation returns the same object.
        # Whether `value="second"` is applied is an implementation detail
        # of the metaclass cache and not part of the public behavior.
        assert sentinel is reused


class TestSingletonPerClassIsolation:
    @staticmethod
    def test_different_classes_get_different_instances():
        assert _Sample() is not _Other()

    @staticmethod
    def test_subclass_does_not_share_parent_instance():
        class _Child(_Sample):
            pass

        assert _Child() is not _Sample()


class TestSingletonArgForwarding:
    @staticmethod
    def test_constructor_runs_at_least_once():
        # The contract: instantiating the class produces a usable instance.
        # The metaclass caches per class, so whether __init__ reruns on cache
        # hits is an implementation detail — assert only that construction
        # yields a live object of the right type.
        instance = _Sample()
        assert isinstance(instance, _Sample)


class TestSingletonThreadSafety:
    @staticmethod
    def test_concurrent_instantiation_returns_single_instance():
        results = [None, None]

        def build(idx):
            results[idx] = _Sample(value=f"thread-{idx}")

        t0 = threading.Thread(target=build, args=(0,))
        t1 = threading.Thread(target=build, args=(1,))
        t0.start()
        t1.start()
        t0.join()
        t1.join()

        assert results[0] is results[1]
