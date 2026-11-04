from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from jiuwen_memory.api import DeleteMode, DeleteSelector, MemoryPatch, Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.type_def import (
    MemoryTier,
    MemoryUnit,
    Modality,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.engine_impl.agent_engine import AgentEngine
from jiuwen_memory.control.engine_impl.cloud_engine import CloudEngine
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.engine_impl.temporal_support import valid_at
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.unit.api.fixtures import security_for


class _VersionFamilyHarness:
    """为三个 Engine 的公开 ``get`` 路径提供确定性的版本族。"""

    def __init__(self, units: list[MemoryUnit]) -> None:
        self.units = units

    def _version_family(self, scope: Scope, unit_id: str) -> list[MemoryUnit]:
        return self.units


class _TestInMemoryEngine(_VersionFamilyHarness, InMemoryEngine):
    pass


class _TestCloudEngine(_VersionFamilyHarness, CloudEngine):
    pass


class _TestAgentEngine(_VersionFamilyHarness, AgentEngine):
    pass


def test_get_as_of_returns_version_valid_at_that_time() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    first_valid = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    second_valid = datetime(2026, 6, 17, 11, 0, tzinfo=UTC)
    old = MemoryUnit(
        id="home-v1",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Shanghai", source=Modality.TEXT)],
        temporal=Temporal(t_valid=first_valid, t_invalid=second_valid),
    )
    new = MemoryUnit(
        id="home-v2",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Beijing", source=Modality.TEXT)],
        temporal=Temporal(t_valid=second_valid),
        supersedes=old.id,
    )
    kv.insert(scope, memory_key(old.id), dumps(old))
    kv.insert(scope, memory_key(new.id), dumps(new))

    before_update = kernel.api.get(
        new.id,
        scope,
        security=security_for(kernel.api, actor),
        as_of=datetime(2026, 6, 17, 10, 30, tzinfo=UTC),
    )
    after_update = kernel.api.get(
        old.id,
        scope,
        security=security_for(kernel.api, actor),
        as_of=datetime(2026, 6, 17, 11, 30, tzinfo=UTC),
    )

    assert before_update.id == old.id
    assert before_update.content == "home is Shanghai"
    assert after_update.id == new.id
    assert after_update.content == "home is Beijing"


def test_get_as_of_handles_historical_update_before_original_write_time() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)

    old = kernel.api.add("home is Shanghai", scope, security=security_for(kernel.api, actor))[0]
    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(
            content="home is Beijing",
            t_valid=datetime(2026, 6, 17, 11, 0, tzinfo=UTC),
        ),
        security=security_for(kernel.api, actor),
    )

    before_update = kernel.api.get(
        new.id,
        scope,
        security=security_for(kernel.api, actor),
        as_of=datetime(2026, 6, 17, 10, 30, tzinfo=UTC),
    )
    after_update = kernel.api.get(
        old.id,
        scope,
        security=security_for(kernel.api, actor),
        as_of=datetime(2026, 6, 17, 11, 30, tzinfo=UTC),
    )

    assert before_update.id == old.id
    assert before_update.content == "home is Shanghai"
    assert after_update.id == new.id
    assert after_update.content == "home is Beijing"


def test_get_as_of_does_not_return_forgotten_version() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    old_valid = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    new_valid = datetime(2026, 6, 17, 11, 0, tzinfo=UTC)

    old = MemoryUnit(
        id="home-v1",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Shanghai", source=Modality.TEXT)],
        temporal=Temporal(t_valid=old_valid, t_invalid=new_valid),
    )
    new = MemoryUnit(
        id="home-v2",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Beijing", source=Modality.TEXT)],
        temporal=Temporal(t_valid=new_valid),
        supersedes=old.id,
    )
    kv.insert(scope, memory_key(old.id), dumps(old))
    kv.insert(scope, memory_key(new.id), dumps(new))
    kernel.api.delete(
        DeleteSelector(unit_ids=[old.id], scope=scope, mode=DeleteMode.FORGET),
        security=security_for(kernel.api, actor),
    )

    with pytest.raises(NotFoundError):
        kernel.api.get(
            new.id,
            scope,
            security=security_for(kernel.api, actor),
            as_of=datetime(2026, 6, 17, 10, 30, tzinfo=UTC),
        )


def test_get_as_of_accepts_naive_validity_window_with_aware_moment() -> None:
    """naive 有效期窗口 + aware as_of：按 UTC 归一后回溯到旧版本，不得抛 TypeError。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    old_valid = datetime(2026, 6, 17, 10, 0)  # naive
    new_valid = datetime(2026, 6, 17, 11, 0)  # naive
    old = MemoryUnit(
        id="home-v1",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Shanghai", source=Modality.TEXT)],
        temporal=Temporal(t_valid=old_valid, t_invalid=new_valid),
    )
    new = MemoryUnit(
        id="home-v2",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Beijing", source=Modality.TEXT)],
        temporal=Temporal(t_valid=new_valid),
        supersedes=old.id,
    )
    kv.insert(scope, memory_key(old.id), dumps(old))
    kv.insert(scope, memory_key(new.id), dumps(new))

    before_update = kernel.api.get(
        new.id,
        scope,
        security=security_for(kernel.api, actor),
        as_of=datetime(2026, 6, 17, 10, 30, tzinfo=UTC),  # aware
    )
    after_update = kernel.api.get(
        old.id,
        scope,
        security=security_for(kernel.api, actor),
        as_of=datetime(2026, 6, 17, 11, 30, tzinfo=UTC),  # aware
    )

    assert before_update.id == old.id, "as_of 落在有效期内应回溯到旧版本"
    assert after_update.id == new.id


def test_get_as_of_naive_moment_outside_window_raises_not_found() -> None:
    """naive as_of 落在有效期之外应出干净的 NotFoundError，而不是 TypeError/InternalError。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    unit = MemoryUnit(
        id="home-v1",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Shanghai", source=Modality.TEXT)],
        temporal=Temporal(t_valid=datetime(2026, 6, 17, 10, 0, tzinfo=UTC)),
    )
    kv.insert(scope, memory_key(unit.id), dumps(unit))

    with pytest.raises(NotFoundError):
        kernel.api.get(
            unit.id,
            scope,
            security=security_for(kernel.api, actor),
            as_of=datetime(2020, 6, 15, 0, 0),  # naive
        )


SCOPE = Scope(org="acme", user="u1", agent="a1", session="s1")

# 同一时刻的两种合法 ISO 形态：naive 按 UTC 解释，aware 带 +00:00。
NAIVE_9 = datetime(2026, 6, 17, 9, 0)
AWARE_9 = datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
NAIVE_10 = datetime(2026, 6, 17, 10, 0)
AWARE_10 = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)



def _windowed(t_valid: datetime | None, t_invalid: datetime | None) -> MemoryUnit:
    return MemoryUnit(
        id="u1",
        scope=SCOPE,
        segments=[Segment(content="note")],
        temporal=Temporal(t_valid=t_valid, t_invalid=t_invalid),
    )


@pytest.mark.parametrize(
    ("t_valid", "t_invalid", "as_of", "expected"),
    [
        (NAIVE_9, NAIVE_10, AWARE_9, True),  # 记忆侧 naive、查询侧 aware
        (AWARE_9, AWARE_10, NAIVE_9, True),  # 记忆侧 aware、查询侧 naive
        (NAIVE_9, AWARE_10, AWARE_9, True),  # 同一 unit 内 t_valid/t_invalid 形态不一致
        (AWARE_9, NAIVE_10, NAIVE_9, True),
        (NAIVE_9, NAIVE_10, AWARE_10, False),  # t_invalid 上界：as_of >= t_invalid 失效
        (AWARE_9, AWARE_10, NAIVE_10, False),
    ],
)
def test_valid_at_compares_naive_and_aware_forms(
    t_valid: datetime,
    t_invalid: datetime,
    as_of: datetime,
    expected: bool,
) -> None:
    assert valid_at(_windowed(t_valid, t_invalid), as_of) is expected, (
        "as_of 与有效期窗口必须按 UTC 归一后比较"
    )


def test_valid_at_non_positive_window_with_mixed_forms() -> None:
    """非正有效期窗口（t_invalid <= t_valid）沿用引擎特例，且两侧形态不一致时不抛错。"""
    unit = _windowed(t_valid=AWARE_10, t_invalid=NAIVE_9)

    assert valid_at(unit, NAIVE_9) is False, "as_of == t_invalid 时非正窗口已失效"
    assert valid_at(unit, datetime(2026, 6, 17, 8, 30)) is True, "as_of < t_invalid 时有效"


def test_valid_at_open_ended_validity_window() -> None:
    unit = _windowed(t_valid=NAIVE_9, t_invalid=None)

    assert valid_at(unit, AWARE_10) is True, "未设 t_invalid 的开放窗口在 t_valid 之后恒有效"
    assert valid_at(unit, datetime(2026, 6, 17, 8, 0, tzinfo=UTC)) is False, (
        "早于 t_valid 的 as_of 无效"
    )


@pytest.mark.parametrize(
    "engine_type", [_TestInMemoryEngine, _TestCloudEngine, _TestAgentEngine]
)
@pytest.mark.parametrize(
    ("older", "newer"),
    [
        (NAIVE_9, AWARE_10),
        (AWARE_9, NAIVE_10),
        (None, NAIVE_10),
        # 本地钟面更晚，实际 UTC 时刻更早，必须按实际时刻选版本。
        (datetime(2026, 6, 17, 16, tzinfo=timezone(timedelta(hours=8))), NAIVE_9),
    ],
)
def test_get_as_of_sorts_mixed_timezone_versions(
    engine_type: type[MemoryEngine], older: datetime | None, newer: datetime
) -> None:
    """三个引擎的真实 get 路径：多个有效候选排序时也必须归一时区。"""
    units = [
        MemoryUnit(
            id=unit_id,
            scope=SCOPE,
            segments=[Segment(content=unit_id)],
            temporal=Temporal(t_valid=t_valid),
        )
        for unit_id, t_valid in (("older", older), ("newer", newer))
    ]
    engine = engine_type(units)
    original_times = [unit.temporal.t_valid for unit in units]

    selected = asyncio.run(engine.get("newer", SCOPE, as_of=AWARE_10))

    assert selected.id == "newer", "混合时区候选应返回 UTC 生效时间最新的版本"
    assert [unit.temporal.t_valid for unit in units] == original_times, "比较不得改写原时间"
