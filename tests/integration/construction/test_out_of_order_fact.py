"""W-01.2: Evolver -> real IndexBuilder -> in-memory KV/fulltext/vector."""

import json
import re
from datetime import UTC, datetime
from itertools import permutations

import pytest

from jiuwen_memory.common.type_def import DedupDecision, LifecycleState, memory_key
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.common.type_def.retrieval_filter import passes_lifecycle
from jiuwen_memory.construction.dedup_impl.keyword_dedup import KeywordDedup
from jiuwen_memory.construction.evolver import EvolveResult
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import (
    ConsolidateDecision,
    DynamicEvolver,
)
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.construction.index_builder_impl.unified_index_builder import UnifiedIndexBuilder
from jiuwen_memory.construction.prompt_registry import PromptRegistry
from tests.conftest import make_storage
from tests.unit.construction.fixtures import create_test_plugins, create_test_stores
from tests.unit.construction.test_dynamic_extraction_consolidation import (
    _ScriptedLLM,
    _unit,
)

pytestmark = pytest.mark.integration


def _setup(kind, unified=False, *, llm=None, prompts=None):
    stores = create_test_stores()
    plugins = create_test_plugins()
    manager = make_storage(**stores)
    builder = (
        UnifiedIndexBuilder(manager.domain_store(), vector_enabled=False)
        if unified else HybridIndexBuilder(manager, plugins["chunker"], plugins["embedder"])
    )
    options = {}
    if prompts is not None:
        options["prompt_registry"] = prompts
    evolver = kind(
        extractor=object(), abstractor=object(), associator=object(),
        index_builder=builder, storage=manager, message_store=stores["kv"],
        dedup=KeywordDedup(manager),
        llm=llm if llm is not None else _ScriptedLLM(
            [json.dumps({"decision": "supersede"})] * 20
        ),
        dedup_high_similarity=1.1,
        **options,
    )
    return evolver, stores, builder


@pytest.mark.parametrize("kind", [OrchestratingEvolver, DynamicEvolver])
@pytest.mark.parametrize("order", list(permutations([1, 2, 3])))
def test_all_write_orders_keep_latest_statement_current(kind, order):
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, stores, _ = _setup(kind)
    result = EvolveResult()
    for day in order:
        candidate = _unit(str(day), "Alice lives in city")
        candidate.temporal.t_message = datetime(2026, 5, day, tzinfo=UTC)
        candidate.temporal.t_valid = datetime(2026, 6, 1, tzinfo=UTC)
        evolver._apply_decision(
            candidate, *evolver._dedup_single(candidate), result
        )
    scope = candidate.scope
    units = [loads(stores["kv"].get(scope, memory_key(str(day)))) for day in [1, 2, 3]]
    assert [unit.id for unit in units if passes_lifecycle(unit, None)] == ["3"]
    docs = stores["fulltext"].get(scope, ["1", "2", "3"])
    assert len(docs) == 3
    for unit, doc in zip(units, docs, strict=True):
        assert doc.metadata["lifecycle"] == unit.lifecycle.value
        assert stores["vector"].get(scope, [f"{unit.id}-0"])[0].metadata["lifecycle"] == (
            unit.lifecycle.value
        )


def test_batch_initial_empty_recall_is_rechecked_after_each_write():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, stores, _ = _setup(OrchestratingEvolver)
    candidates = [_unit(str(day), "Alice lives in city") for day in [3, 1, 2]]
    for candidate in candidates:
        candidate.temporal.t_message = datetime(2026, 5, int(candidate.id), tzinfo=UTC)
    result = evolver._dedup_batch(candidates)
    assert result.created_ids == ["3", "1", "2"]
    units = [loads(stores["kv"].get(c.scope, memory_key(c.id))) for c in candidates]
    assert [unit.id for unit in units if unit.lifecycle == LifecycleState.ACTIVE] == ["3"]


def test_unified_builder_also_persists_reverse_expiry():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, stores, builder = _setup(OrchestratingEvolver, unified=True)

    new, old = _unit("new", "Alice lives in Shanghai"), _unit("old", "Alice lived in Beijing")
    new.temporal.t_message = datetime(2026, 6, 1, tzinfo=UTC)
    old.temporal.t_message = datetime(2026, 5, 1, tzinfo=UTC)
    builder.build([new])
    evolver._apply_decision(old, DedupDecision.SUPERSEDE, new, 0.8, EvolveResult())
    assert loads(stores["kv"].get(old.scope, memory_key(old.id))).lifecycle == (
        LifecycleState.SUPERSEDED
    )


def test_dynamic_batch_recheck_keeps_dynamic_prompt_and_target_contract():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    class TargetLLM(_ScriptedLLM):
        def chat(self, messages, **kwargs):
            assert messages[0].content == "dynamic test policy"
            target = re.search(r"\[Memory ID: ([^\]]+)\]", messages[1].content)
            return json.dumps({"decision": "supersede", "existing_id": target.group(1)})

    prompts = PromptRegistry.from_dict({"consolidate": {"key": "dynamic test policy"}})
    evolver, stores, _ = _setup(DynamicEvolver, llm=TargetLLM(), prompts=prompts)
    candidates = [_unit(str(day), "Alice lives in city", {
        "_extraction_strategy": "test",
        "_consolidation_prompt_test": "key",
    }) for day in [3, 1, 2]]
    for candidate in candidates:
        candidate.temporal.t_message = datetime(2026, 5, int(candidate.id), tzinfo=UTC)
    decisions = [ConsolidateDecision(c, DedupDecision.ADD, None, 0) for c in candidates]
    result = evolver._persist_decisions(candidates, decisions)
    assert result.created_ids == ["3", "1", "2"]
    units = [loads(stores["kv"].get(c.scope, memory_key(c.id))) for c in candidates]
    assert [unit.id for unit in units if unit.lifecycle == LifecycleState.ACTIVE] == ["3"]
