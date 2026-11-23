"""Bounded openGauss pool reuse, admission and rotation contracts."""

from __future__ import annotations

# pylint: disable=protected-access  # Fault injection verifies capacity and active-lease cleanup.
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError
from jiuwen_memory.storage._opengauss import _ConnectionPool
from jiuwen_memory.storage.vector_impl.opengauss_vector import OpenGaussVectorStore
from tests.unit.storage.opengauss_fixtures import RecordingConnection, RecordingPool

pytestmark = pytest.mark.unit


class Connection(RecordingConnection):
    def __init__(self):
        super().__init__()
        self.status = 0

    def get_transaction_status(self):
        return self.status

    def rollback(self):
        self.rollbacks += 1
        self.status = 0

    def close(self):
        self.closed = True


def pool_factory(*, maximum=3, timeout=0.05):
    created = []

    def connect():
        conn = Connection()
        created.append(conn)
        return conn

    return _ConnectionPool(1, maximum, connect=connect, timeout=timeout), created


def test_repeated_bursts_retain_all_healthy_connections():
    pool, created = pool_factory()
    try:
        for _ in range(20):
            borrowed = [pool.getconn() for _ in range(3)]
            assert len({id(conn) for conn in borrowed}) == 3
            for conn in borrowed:
                pool.putconn(conn)
        assert len(created) == 3
        assert not any(conn.closed for conn in created)
    finally:
        pool.closeall()
    assert all(conn.closed for conn in created)


def test_concurrent_total_never_exceeds_capacity():
    pool, created = pool_factory(maximum=3, timeout=3)
    barrier = threading.Barrier(3)

    def borrow():
        conn = pool.getconn()
        try:
            barrier.wait(timeout=2)
            return id(conn)
        finally:
            pool.putconn(conn)

    with ThreadPoolExecutor(max_workers=3) as threads:
        assert len(set(threads.map(lambda _: borrow(), range(3)))) == 3
    assert len(created) == 3
    pool.closeall()


def test_partial_initialization_failure_closes_precreated_connections():
    first = Connection()
    attempts = iter([first, KeyboardInterrupt()])

    def connect():
        item = next(attempts)
        if isinstance(item, BaseException):
            raise item
        return item

    with pytest.raises(KeyboardInterrupt):
        _ConnectionPool(2, 2, connect=connect, timeout=1)
    assert first.closed


def test_exhaustion_waits_only_for_configured_timeout():
    pool, _ = pool_factory(maximum=1)
    conn = pool.getconn()
    try:
        with pytest.raises(BackendError, match="timed out"):
            pool.getconn()
        pool.putconn(conn)
        assert pool.getconn() is conn
    finally:
        pool.putconn(conn)
        pool.closeall()


def test_returned_connection_serves_a_waiting_borrower():
    pool, created = pool_factory(maximum=1, timeout=2)
    conn = pool.getconn()
    with ThreadPoolExecutor(max_workers=1) as threads:
        waiting = threads.submit(pool.getconn)
        pool.putconn(conn)
        assert waiting.result(timeout=2) is conn
    pool.putconn(conn)
    pool.closeall()
    assert len(created) == 1


def test_slow_connect_does_not_block_returns_or_idle_borrows():
    pool, created = pool_factory(maximum=2, timeout=2)
    first = pool.getconn()
    connecting, release = threading.Event(), threading.Event()
    original = pool._connect

    def slow_connect():
        connecting.set()
        assert release.wait(3)
        return original()

    pool._connect = slow_connect
    with ThreadPoolExecutor(max_workers=2) as threads:
        pending = threads.submit(pool.getconn)
        try:
            assert connecting.wait(2)
            threads.submit(pool.putconn, first).result(timeout=1)
            assert threads.submit(pool.getconn).result(timeout=1) is first
        finally:
            release.set()
        second = pending.result(timeout=2)
    pool.putconn(first)
    pool.putconn(second)
    pool.closeall()
    assert len(created) == 2


def test_connect_failure_releases_reserved_capacity_even_for_base_exception():
    pool, _ = pool_factory(maximum=2)
    first = pool.getconn()
    original = pool._connect

    def fail():
        raise KeyboardInterrupt()

    pool._connect = fail
    with pytest.raises(KeyboardInterrupt):
        pool.getconn()
    pool._connect = original
    second = pool.getconn()
    pool.putconn(first)
    pool.putconn(second)
    pool.closeall()


def test_return_discards_closed_connections_and_resets_dirty_transactions():
    pool, created = pool_factory(maximum=1)
    first = pool.getconn()
    first.status = 2
    pool.putconn(first)
    assert first.rollbacks == 1
    assert pool.getconn() is first
    first.closed = True
    pool.putconn(first)
    second = pool.getconn()
    assert second is not first
    pool.putconn(second)
    pool.closeall()
    assert len(created) == 2


def test_failed_reset_discards_session_without_leaking_capacity():
    pool, _ = pool_factory(maximum=1)
    first = pool.getconn()
    first.status = 3

    def fail():
        raise RuntimeError("socket lost")

    first.rollback = fail
    pool.putconn(first)
    assert first.closed
    second = pool.getconn()
    assert second is not first
    pool.putconn(second)
    pool.closeall()


def test_failed_close_logs_without_exposing_driver_details_or_leaking_capacity(caplog):
    pool, created = pool_factory(maximum=1)
    first = pool.getconn()

    def fail_close():
        raise RuntimeError("postgresql://user:secret@example.invalid/memory")

    first.close = fail_close
    with caplog.at_level("WARNING"):
        pool.putconn(first, close=True)
    assert "openGauss connection close failed" in caplog.text
    assert "secret" not in caplog.text and "example.invalid" not in caplog.text
    second = pool.getconn()
    assert second is not first and len(created) == 2
    pool.putconn(second)
    pool.closeall()


def test_close_failure_does_not_mask_the_original_transaction_error(monkeypatch):
    pool, created = pool_factory(maximum=1)
    store = OpenGaussVectorStore(dsn="host=unused", dim=3)
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)

    def fail_close():
        raise RuntimeError("close failed")

    monkeypatch.setattr(created[0], "close", fail_close)
    with pytest.raises(ConflictError, match="original error"):
        with store.transaction():
            store.close()
            raise ConflictError(message="original error")
    with pytest.raises(BackendError, match="closed"):
        pool.getconn()


def test_closed_idle_session_is_replaced_before_use():
    pool, created = pool_factory(maximum=1)
    created[0].closed = True
    conn = pool.getconn()
    assert len(created) == 2 and not conn.closed
    pool.putconn(conn)
    pool.closeall()


def test_close_drains_borrowers_and_rejects_new_admission():
    pool, _ = pool_factory(maximum=1)
    conn = pool.getconn()
    pool.closeall()
    assert not conn.closed
    with pytest.raises(BackendError, match="closed"):
        pool.getconn()
    pool.putconn(conn)
    assert conn.closed
    pool.closeall()


def test_close_during_connect_disposes_new_session_and_releases_slot():
    pool, _ = pool_factory(maximum=2, timeout=2)
    first = pool.getconn()
    connecting, release = threading.Event(), threading.Event()

    def slow_connect():
        connecting.set()
        assert release.wait(3)
        return Connection()

    pool._connect = slow_connect
    with ThreadPoolExecutor(max_workers=1) as threads:
        pending = threads.submit(pool.getconn)
        try:
            assert connecting.wait(2)
            pool.closeall()
        finally:
            release.set()
        with pytest.raises(BackendError, match="closed"):
            pending.result(timeout=2)
    pool.putconn(first)
    assert first.closed


def test_double_return_is_rejected_without_corrupting_capacity():
    pool, _ = pool_factory(maximum=1)
    conn = pool.getconn()
    pool.putconn(conn)
    with pytest.raises(BackendError, match="not borrowed"):
        pool.putconn(conn)
    assert pool.getconn() is conn
    pool.putconn(conn)
    pool.closeall()


def test_borrow_failure_does_not_leave_an_active_lease(monkeypatch):
    store = OpenGaussVectorStore(dsn="host=unused", dim=3)
    pool = RecordingPool()

    def fail():
        raise RuntimeError("borrow failed")

    pool.getconn = fail
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    with pytest.raises(BackendError, match="borrow failed"):
        with store.transaction():
            pass
    assert store._lease.active == 0
    store.close()
    assert pool.closed == 1


def test_store_waiting_borrow_does_not_block_the_returning_transaction(monkeypatch):
    pool, _ = pool_factory(maximum=1, timeout=2)
    store = OpenGaussVectorStore(dsn="host=unused", dim=3)
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    ctx = store.transaction()
    ctx.__enter__()
    entered = threading.Event()

    def request():
        entered.set()
        with store.transaction():
            pass

    with ThreadPoolExecutor(max_workers=2) as threads:
        pending = threads.submit(request)
        try:
            assert entered.wait(1)
            threads.submit(ctx.__exit__, None, None, None).result(timeout=1)
        finally:
            pending.result(timeout=3)
    assert store._lease.active == 0
    store.close()


def test_base_exception_rolls_back_and_returns_active_lease(monkeypatch):
    store = OpenGaussVectorStore(dsn="host=unused", dim=3)
    pool = RecordingPool()
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    with pytest.raises(KeyboardInterrupt):
        with store.transaction():
            raise KeyboardInterrupt()
    assert pool.connection.rollbacks == 1 and len(pool.returns) == 1
    assert store._lease.active == 0
    store.close()


def test_slow_rotation_does_not_hold_up_old_lease_return(monkeypatch):
    store = OpenGaussVectorStore(dsn="host=old", dim=3)
    old, new = RecordingPool(), RecordingPool()
    dsn = ["host=old"]
    connecting, release = threading.Event(), threading.Event()

    def new_pool(value):
        if value == "host=old":
            return old
        connecting.set()
        assert release.wait(3)
        return new

    monkeypatch.setattr(store, "_new_pool", new_pool)
    monkeypatch.setattr(store, "_resolved_dsn", lambda: dsn[0])
    ctx = store.transaction()
    ctx.__enter__()
    dsn[0] = "host=new"

    def request():
        with store.transaction():
            pass

    with ThreadPoolExecutor(max_workers=2) as threads:
        pending = threads.submit(request)
        try:
            assert connecting.wait(2)
            threads.submit(ctx.__exit__, None, None, None).result(timeout=1)
        finally:
            release.set()
        pending.result(timeout=2)
    assert old.closed == 1
    store.close()
    assert new.closed == 1


def test_released_connection_survives_a_business_error(monkeypatch):
    pool, created = pool_factory(maximum=1)
    store = OpenGaussVectorStore(dsn="host=unused", dim=3)
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    with pytest.raises(ConflictError):
        with store.transaction():
            raise ConflictError()
    with store.transaction():
        pass
    assert len(created) == 1 and created[0].rollbacks == 1
    store.close()
