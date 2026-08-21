"""Ingest job controller implementations."""

from importlib import import_module

from jiuwen_memory.control.ingest_job import IngestJobProducer

import_module(".ingest_job", __name__)

__all__ = ["IngestJobProducer"]
