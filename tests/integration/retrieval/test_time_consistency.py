# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入、结构区间、索引和时间过滤在不同进程时区下使用同一时间基准。"""

import time
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.time_utils import as_utc, epoch_ms
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryUnit,
    ParsedQuery,
    Segment,
    Temporal,
    is_retrieval_candidate,
    matches_memory_unit,
    normalize,
)
from jiuwen_memory.common.type_def.filter import and_merge
from jiuwen_memory.construction.index_builder_impl._index_ops import index_metadata
from jiuwen_memory.construction.index_builder_impl.unified_index_builder import UnifiedIndexBuilder
from jiuwen_memory.retrieval.retriever_impl.predicate_builder import build_system_filters
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
from jiuwen_memory.storage.types import TextQuery
from tests.conftest import make_storage
from tests.unit.api.fixtures import HOME, ROOT_SECURITY, runtime_config, security_for

pytestmark = pytest.mark.integration


@pytest.fixture(params=["UTC0", "UTC-8", "EST5EDT,M3.2.0,M11.1.0"])
def process_timezone(request, monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("测试需要支持进程时区切换")
    try:
        with monkeypatch.context() as env:
            env.setenv("TZ", request.param)
            time.tzset()
            yield
    finally:
        time.tzset()


@pytest.fixture(params=[
    "2026-03-08T02:30:00.123456",  # 美国夏令时切换日的本地不存在时间，不得按本地解释。
    "2026-03-08T10:30:00.123456+08:00",
    "2026-03-07T21:30:00.123456-05:00",
])
def moment(request):
    return datetime.fromisoformat(request.param) + timedelta(seconds=3)


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
@pytest.mark.parametrize("infer", [False, True])
def test_add_preserves_input_time_and_snapshot_interval(process_timezone, moment, engine, infer):
    runtime = assemble_runtime(config=runtime_config(engine))
    try:
        api = runtime.api
        api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        units = api.add(
            "Alice likes climbing", HOME, security=security_for(api, HOME),
            occurred_at=moment, system_metadata={"infer": infer},
        )
        assert units
        for unit in units:
            stored = api.get(unit.id, HOME, security=security_for(api, HOME))
            assert stored.temporal.t_message.isoformat() == moment.isoformat()
            assert stored.hierarchy.span_start == stored.hierarchy.span_end == moment
            assert stored.hierarchy.span_start.isoformat() == moment.isoformat()
    finally:
        runtime.close()


def test_time_projection_and_filters_agree_across_storage_paths(
    process_timezone, moment, tmp_path,
):
    expected = datetime(2026, 3, 8, 2, 30, 3, 123456, tzinfo=UTC)
    expected_ms = int(expected.timestamp() * 1000)
    end = moment + timedelta(seconds=3)
    assert as_utc(moment) == expected
    assert as_utc(moment).microsecond == 123456
    assert epoch_ms(end) - epoch_ms(moment) == 3000
    unit = MemoryUnit(
        id="leaf", scope=HOME, segments=[Segment(content="alpha")],
        temporal=Temporal(t_message=moment, t_event=moment, t_ingest=moment,
                          t_valid=moment, t_invalid=end),
        hierarchy=HierarchyRef(kind=HierarchyKind.TIME, role=HierarchyRole.SNAPSHOT,
                               span_start=moment, span_end=end),
    )
    expected_projection = dict.fromkeys(
        ["t_message", "t_event", "t_ingest", "t_valid", "tree_span_start"], expected_ms,
    )
    expected_projection.update(t_invalid=expected_ms + 3000, tree_span_end=expected_ms + 3000)
    for layer in ("l0", "l1", "l2"):
        projected = index_metadata(unit, layer=layer)
        assert {key: projected[key] for key in expected_projection} == expected_projection
    assert matches_memory_unit(unit, normalize(expected_projection))
    wrong = {"t_message": expected_ms - 8 * 3600 * 1000}
    assert not matches_memory_unit(unit, normalize(wrong))

    domain = make_storage(kv=InMemoryKVStore()).domain_store()
    UnifiedIndexBuilder(domain, vector_enabled=False).build([unit])
    stored = domain.get(HOME, [unit.id])[0]
    for field in ("t_event", "t_valid", "t_invalid", "tree_span_start", "tree_span_end"):
        assert stored.system_metadata[field] == expected_projection[field]
    assert stored.temporal.t_message.isoformat() == moment.isoformat()

    shadow = SqliteDocumentShadowIndex(
        db_path=str(tmp_path / "time.db"), tokenizer=WhitespaceTokenizer(),
    )
    try:
        shadow.insert_units(HOME, [unit])
        for start in (moment, expected, expected.replace(tzinfo=None)):
            filters = and_merge(
                None, build_system_filters(start, start, start + timedelta(seconds=3)),
            )
            assert matches_memory_unit(unit, filters)
            assert is_retrieval_candidate(
                unit,
                ParsedQuery(as_of=start, time_from=start, time_to=start + timedelta(seconds=3)),
                filters=filters,
            )
            hits = shadow.search_fulltext(HOME, TextQuery(text="alpha", filters=filters))
            assert [hit.id for hit in hits] == [unit.id]
        after = expected + timedelta(seconds=3)
        filters = and_merge(None, build_system_filters(after, after, None))
        assert not matches_memory_unit(unit, filters)
        assert shadow.search_fulltext(HOME, TextQuery(text="alpha", filters=filters)) == []
    finally:
        shadow.close()
