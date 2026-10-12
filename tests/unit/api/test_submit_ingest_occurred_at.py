# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``submit_ingest`` 的 ``occurred_at`` 必须原样交给后台 ``add``。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import Modality, Scope
from jiuwen_memory.api.memory_api_impl.admin_ops import AdminOpsMixin

pytestmark = pytest.mark.unit


class _IngestJobs:
    def __init__(self) -> None:
        self.task = None

    def submit(self, *, payload_id, source_ref, scope, task):
        del payload_id, source_ref, scope
        self.task = task
        return SimpleNamespace(job=SimpleNamespace(id="j1"), reused=False)


class _Api(AdminOpsMixin):
    def __init__(self) -> None:
        self._ingest_jobs = _IngestJobs()
        self.added: list = []

    def check_write(
        self, scope, security, *, tags=None, system_metadata=None, user_metadata=None
    ):
        del scope, security, tags, system_metadata, user_metadata

    def add(self, content, scope, source, **kwargs):
        del scope
        self.added.append({"content": content, "source": source, **kwargs})
        return []


def test_submit_ingest_forwards_occurred_at_to_background_add() -> None:
    """消息发生时间属于 ``occurred_at``，不借 metadata 旁路；缺链会让后台写入丢失时间轴。"""
    api = _Api()
    occurred_at = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)

    api.submit_ingest(
        "doc",
        Scope(org="acme"),
        Modality.TEXT,
        security=None,
        payload_id="p1",
        source_ref="file:///a.pdf",
        occurred_at=occurred_at,
    )
    api._ingest_jobs.task()

    assert api.added[0]["occurred_at"] is occurred_at


def test_submit_ingest_leaves_occurred_at_unset_by_default() -> None:
    api = _Api()

    api.submit_ingest(
        "doc",
        Scope(org="acme"),
        Modality.TEXT,
        security=None,
        payload_id="p1",
        source_ref="file:///a.pdf",
    )
    api._ingest_jobs.task()

    assert api.added[0]["occurred_at"] is None
