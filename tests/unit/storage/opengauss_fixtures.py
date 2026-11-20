"""Recording DB-API doubles for the openGauss adapter contracts."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager


class RecordingCursor:
    def __init__(self, *, one=(), batches=()) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.one = deque(one)
        self.batches = deque(batches)
        self.failure: tuple[str, Exception] | None = None

    def execute(self, sql, params=()) -> None:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, tuple(params)))
        if self.failure is not None and normalized.startswith(self.failure[0]):
            raise self.failure[1]

    def fetchone(self):
        return self.one.popleft() if self.one else None

    def fetchall(self):
        return self.batches.popleft() if self.batches else []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecordingConnection:
    def __init__(self, cursor=None) -> None:
        self.recording_cursor = cursor or RecordingCursor()
        self.closed = False
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.recording_cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commits += 1
        else:
            self.rollbacks += 1
        return False


class RecordingPool:
    def __init__(self, cursor=None) -> None:
        self.connection = RecordingConnection(cursor)
        self.returns = []
        self.closed = 0

    def getconn(self):
        return self.connection

    def putconn(self, connection, close=False):
        self.returns.append((connection, close))

    def closeall(self):
        self.closed += 1


def bind_cursor(monkeypatch, store, cursor):
    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(store, "transaction", transaction)
    return cursor
