# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``submit_ingest`` 的 ``occurred_at`` 必须原样交给后台 ``add``。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import Modality, Scope
from jiuwen_memory.api.memory_api_impl.admin_ops import AdminOpsMixin
from jiuwen_memory.api.memory_api_impl.pep_ops import PepOpsMixin

pytestmark = pytest.mark.unit


class _IngestJobs:
    """后台摄入任务的替身：内联执行 task，使转发结果可在同一栈内断言。"""

    def __init__(self, owner) -> None:
        self.owner = owner

    def submit(self, *, payload_id, source_ref, scope, task):
        del payload_id, source_ref, scope
        # 真实控制器把 task 排入后台队列；替身就地执行，于是测试无需触碰受保护的
        # ``_ingest_jobs`` 去手动触发排队任务，提交次数经公开计数即可观测。
        self.owner.ingested += 1
        task()
        return SimpleNamespace(job=SimpleNamespace(id="j1"), reused=False)


class _Api(AdminOpsMixin, PepOpsMixin):
    def __init__(self) -> None:
        self.ingested = 0
        self._ingest_jobs = _IngestJobs(self)
        # 普通审计部署也走真实 PEP 预检；不以缺少方法的替身跳过该边界。
        self._audit_integrity = None
        self.added: list = []

    @staticmethod
    def check_write(scope, security, *, tags=None, system_metadata=None, user_metadata=None):
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

    assert api.ingested == 1
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

    assert api.added[0]["occurred_at"] is None
