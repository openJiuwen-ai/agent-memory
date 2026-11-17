# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""共享文档资源串行、独立资源并行以及嵌套/异常释放。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.storage import sync_gate
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
from jiuwen_memory.storage.sync_gate import document_operation, resource_path_key, write_window_open

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "same_root,same_db", [(True, True), (True, False), (False, True), (False, False)],
)
def test_operations_coordinate_only_shared_resources(tmp_path, same_root, same_db):
    """共享任一资源时互斥，完全独立时允许同时进入写窗口。"""
    first = (str(tmp_path / "a"), resource_path_key("sqlite", str(tmp_path / "a.db")))
    second = (
        first[0] if same_root else str(tmp_path / "b"),
        first[1] if same_db else resource_path_key("sqlite", str(tmp_path / "b.db")),
    )
    attempted, entered = threading.Event(), threading.Event()

    def run():
        attempted.set()
        with document_operation(*second, writing=True):
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with document_operation(*first, writing=True):
            assert write_window_open(*second) is (same_root or same_db)
            future = pool.submit(run)
            assert attempted.wait(2), "worker did not start"
            assert entered.wait(0.1 if same_root or same_db else 2) is not (same_root or same_db)
        future.result(timeout=2)
    assert entered.is_set()
    assert not write_window_open()


def test_nested_failure_releases_only_inner_window(tmp_path):
    """内层异常不提前关掉外层写窗口，全部退出后其他线程可取得锁。"""
    resources = (str(tmp_path), "test-index")
    with document_operation(*resources, writing=True):
        with pytest.raises(RuntimeError, match="injected"):
            with document_operation(*resources, writing=True):
                raise RuntimeError("injected")
        assert write_window_open(*resources)
    assert not write_window_open(*resources)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def acquire_again():
            with document_operation(*resources):
                return True
        assert pool.submit(acquire_again).result(timeout=2)


def test_paths_and_memory_connections_use_correct_identity(tmp_path, monkeypatch):
    """路径别名共享资源标识，独立内存连接保持各自标识。"""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert resource_path_key("markdown", "alias") == resource_path_key("markdown", str(real))
    tokenizer = WhitespaceTokenizer()
    first = SqliteDocumentShadowIndex(db_path=str(real / "shadow.db"), tokenizer=tokenizer)
    second = SqliteDocumentShadowIndex(db_path="alias/shadow.db", tokenizer=tokenizer)
    assert first.write_lock_key == second.write_lock_key
    memory_a = SqliteDocumentShadowIndex(db_path=":memory:", tokenizer=tokenizer)
    memory_b = SqliteDocumentShadowIndex(db_path=":memory:", tokenizer=tokenizer)
    assert memory_a.write_lock_key != memory_b.write_lock_key


def test_timeout_releases_partially_acquired_resources(tmp_path, monkeypatch):
    """锁等待有界，第二把锁超时后释放第一把锁，不能留下写窗口。"""
    first_root = str(tmp_path / "first")
    shared_index = "sqlite:shared"
    entered, release = threading.Event(), threading.Event()

    def hold_index():
        with document_operation(str(tmp_path / "other"), shared_index):
            entered.set()
            assert release.wait(5), "lock holder was not released"

    def probe_first_root():
        with document_operation(first_root, "sqlite:independent"):
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_index)
        try:
            assert entered.wait(2), "lock holder did not start"
            monkeypatch.setattr(sync_gate, "MAX_SYNC_DEFERRAL_SECONDS", 0)
            with pytest.raises(BackendError, match="timed out"):
                with document_operation(first_root, shared_index, writing=True):
                    pytest.fail("contended operation must not start")
            assert not write_window_open(first_root, shared_index)
            assert pool.submit(probe_first_root).result(timeout=2)
        finally:
            release.set()
        holder.result(timeout=2)
