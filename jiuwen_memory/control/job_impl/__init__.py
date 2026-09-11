# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Ingest job controller implementations."""

import logging
from importlib import import_module

from jiuwen_memory.control.ingest_job import IngestJobProducer

try:
    import_module(".ingest_job", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["IngestJobProducer"]
