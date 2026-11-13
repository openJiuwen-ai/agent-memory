# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入/检索链路埋点：in-memory 装配下 add/search 在 trace 作用域内产出调用树。"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import assemble
from jiuwen_memory.common.security.request_context import internal_context
from jiuwen_memory.common.trace import begin_trace, end_trace
from jiuwen_memory.common.type_def import Context, Modality, Scope
from jiuwen_memory.config import Config
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")
_TEXT = "alice loves iced americano coffee every single morning before work meetings"


def _walk(nodes: list[dict]):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", []))


def _api():
    return assemble(config=Config.from_dict({}))


def test_add_traces_ingest_and_result() -> None:
    api = _api()
    token = begin_trace("memory")
    try:
        api.add(_TEXT, _SCOPE, source=Modality.TEXT, security=internal_context(
            ScopedAuthenticator(_ACTOR)
        ))
    finally:
        snapshot = end_trace(token)

    (write,) = snapshot["spans"]
    assert write["stage"] == "write" and write["cost_ms"] >= 0, f"Engine.write 应为根 span: {write}"
    ingest, result = write["children"]
    assert ingest["stage"] == "ingest" and ingest["data"] == {
        "units": 1,
        "modality": "text",
        "path": "raw",
    }, f"ingest 埋点: {ingest}"
    assert result["stage"] == "result" and result["data"] == {
        "path": "raw",
        "originals": 1,
        "returned": 1,
    }, f"result 埋点: {result}"


@pytest.mark.parametrize("with_trajectory", [False, True])
def test_search_traces_every_pipeline_stage(with_trajectory: bool) -> None:
    api = _api()
    api.add(_TEXT, _SCOPE, source=Modality.TEXT, security=internal_context(
            ScopedAuthenticator(_ACTOR)
        ))

    token = begin_trace("memory")
    try:
        result = api.search(
            "coffee", Context(_SCOPE),
            security=internal_context(ScopedAuthenticator(_ACTOR)),
            with_trajectory=with_trajectory,
        )
    finally:
        snapshot = end_trace(token)

    retrieve = next((n for n in _walk(snapshot["spans"]) if n["stage"] == "retrieve"), None)
    assert retrieve is not None, f"PipelineRetriever.retrieve 应为 span: {snapshot}"
    stages = {child["stage"] for child in retrieve.get("children", [])}
    assert {"parse", "recall", "fuse", "disclose"} <= stages, (
        f"检索各阶段都应挂在 retrieve span 下（与 with_trajectory 无关）: {sorted(stages)}"
    )
    if with_trajectory:
        assert result.trajectory[0].stage == "pipeline", "引擎保留 pipeline 选择轨迹"
        assert [c["stage"] for c in retrieve["children"]] == [
            step.stage for step in result.trajectory[1:]
        ], "阶段重构后 trace 与 trajectory 应保留相同事件顺序且不重复"
    else:
        assert result.trajectory == [], "关闭 trajectory 时仍应记录过程 trace"
    parse = next(c for c in retrieve["children"] if c["stage"] == "parse")["data"]
    assert parse["candidates"] > 0 and "cost_ms" in parse, f"parse 埋点字段: {parse}"
    recalls = [c["data"] for c in retrieve["children"] if c["stage"] == "recall"]
    assert all("channel" in r for r in recalls), f"recall 事件应带 channel: {recalls}"


def test_no_scope_means_no_trace_side_effect() -> None:
    api = _api()
    units = api.add(_TEXT, _SCOPE, source=Modality.TEXT, security=internal_context(
            ScopedAuthenticator(_ACTOR)
        ))
    assert len(units) == 1, "无 trace 作用域时写入行为不变"
