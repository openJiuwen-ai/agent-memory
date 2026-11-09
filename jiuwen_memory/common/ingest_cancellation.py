"""Task-local cooperative cancellation shared by ingest orchestration and model plugins."""

import time
from contextvars import ContextVar
from threading import Event


class IngestCancelled(Exception):
    """A running ingest observed its cancellation request."""


ingest_cancel_signal: ContextVar[Event | None] = ContextVar("ingest_cancel_signal", default=None)


def check_ingest_cancelled() -> None:
    signal = ingest_cancel_signal.get()
    if signal is not None and signal.is_set():
        raise IngestCancelled("ingest cancellation requested")


def wait_ingest_cancelled(seconds: float) -> None:
    """Wait between polling/retry attempts, waking immediately when cancelled."""
    signal = ingest_cancel_signal.get()
    if signal is None:
        time.sleep(seconds)
    elif signal.wait(seconds):
        check_ingest_cancelled()
