# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Ingest job controller implementations."""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.ingest_job import IngestJobProducer

import_optional(".ingest_job", __name__)

__all__ = ["IngestJobProducer"]
