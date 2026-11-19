"""Public APIs can rebuild repeated TIME roles and navigate beyond four levels."""

from datetime import timedelta

import pytest

from jiuwen_memory.api import Context, JobStatus, assemble_runtime
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.graphmem_structure import node_height, read_child_details
from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, Scope, ScoredMemoryUnit
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker
from jiuwen_memory.retrieval.graph_navigation import GraphEvidenceNavigator, GraphNavigationPolicy
from jiuwen_memory.retrieval.types import DisclosureLevel
from tests.integration.retrieval.test_graphmem_interfaces import tokenizer_file
from tests.unit.api.fixtures import START, security_for
from tests.unit.api.hierarchy_api_fixtures import HOME, runtime_config, task_options, write_snapshot
from tests.unit.construction.graphmem_fixtures import query
from tests.unit.construction.test_recursive_hierarchy import SummaryModel

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
def test_public_multipart_ingest_keeps_order_during_recursive_build(tmp_path, engine):
    filename, _ = tokenizer_file(tmp_path)
    model = SummaryModel()

    @LlmProducer.register("recursive_parts_fixture")
    def build_model(config):
        return model

    config = runtime_config(engine)
    config["globals"]["graph_enabled"] = False
    config["ingestor"] = {"default": {"target": "simple", "params": {"max_unit_chars": 64}}}
    config["llm"] = {"recursive_test": {"target": "recursive_parts_fixture"}}
    config["hierarchy_composer"]["tree"] = {"target": "graphmem_recursive", "params": {
        "index_builder": "default", "llm": "recursive_test", "tokenizer_file": str(filename),
        "recursive_policy": {"max_children": 2},
    }}
    Factory.reset_all()
    runtime = assemble_runtime(config=config)
    try:
        api = runtime.api
        scope = Scope(org=HOME.org, user=HOME.user, agent=HOME.agent, session="multipart")
        text = "\n\n".join(
            f"Source paragraph {i}: distinct chronological evidence." for i in range(12))
        leaves = api.add(text, scope, security=security_for(api, scope),
                         system_metadata={"infer": False}, occurred_at=START)
        assert len(leaves) > 2
        assert "\n\n".join(u.content for u in leaves) == text
        security = security_for(api, HOME)
        job = api.evolve(HOME, **vars(task_options()), security=security)
        assert api.job_status(job, security=security).status is JobStatus.SUCCEEDED
        records = [r for call in model.calls for r in call if r["kind"] == "source"]
        assert [r["text"] for r in records] == [u.content for u in leaves]
        before = len(model.calls)
        job = api.evolve(HOME, **vars(task_options()), security=security)
        assert api.job_status(job, security=security).status is JobStatus.SUCCEEDED
        assert len(model.calls) == before, "a repeated public build must reuse unchanged summaries"
    finally:
        runtime.close()
        Factory.reset_all()


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
@pytest.mark.parametrize("adaptive", [False, True])
def test_recursive_public_build_append_search_and_summary_only(
    tmp_path, monkeypatch, engine, adaptive,
):
    filename, _ = tokenizer_file(tmp_path)
    model = SummaryModel()

    @LlmProducer.register("recursive_fixture_summary")
    def build_model(config):
        return model

    config = runtime_config(engine)
    config["globals"]["graph_enabled"] = False
    config["llm"] = {"recursive_test": {"target": "recursive_fixture_summary"}}
    config["hierarchy_composer"]["tree"] = {"target": "graphmem_recursive", "params": {
        "index_builder": "default", "llm": "recursive_test", "tokenizer_file": str(filename),
        "recursive_policy": {"max_children": 2},
    }}
    config["retriever"] = {"default": {"target": "pipeline", "params": {
        "rerank_enabled": False,
        "evidence_packing": {"tokenizer_file": str(filename), "mode": "sources_first"},
        "graph_navigation": {"strategy": "relational", "seed_count": 8, "source_limit": 128,
                             "max_reads": 512, "ancestor_hops": 64,
                             "initial_candidate_limit": 4},
    }}}
    config["retriever"]["default"]["params"]["graph_navigation"].update(
        source_candidate_ratio=0.1, summary_candidate_ratio=0.25,
        adaptive_budget=adaptive, initial_tokens=256, detail_source_hints=adaptive,
    )
    Factory.reset_all()
    runtime = assemble_runtime(config=config)
    try:
        api, domain = runtime.api, runtime._kernel.storage.domain_store()
        security = security_for(api, HOME)
        sources = [write_snapshot(api, i) for i in range(32)]
        options = task_options(span_end=START + timedelta(hours=1))
        job = api.evolve(HOME, **vars(options), security=security)
        info = api.job_status(job, security=security)
        assert info.status is JobStatus.SUCCEEDED, info
        parents = domain.list(HOME, limit=200).items
        root = max(parents, key=node_height)
        assert node_height(root) == 5
        assert all(len(read_child_details(p)) == len(p.hierarchy.child_ids) for p in parents)
        before = len(model.calls)
        sources.append(write_snapshot(api, 33))
        job = api.evolve(HOME, **vars(options), security=security)
        info = api.job_status(job, security=security)
        assert info.status is JobStatus.SUCCEEDED, info
        assert len(model.calls) == before + 1
        parents = domain.list(HOME, limit=200).items
        root = max(parents, key=node_height)
        materialization = []
        original_get = domain.get

        def record_get(scope, ids, **kwargs):
            materialization.append((scope, list(ids)))
            return original_get(scope, ids, **kwargs)

        monkeypatch.setattr(domain, "get", record_get)
        result = api.search("evidence", Context(HOME, extensions={"max_tokens": 12000}),
                            top_k=256, hierarchy_kind=HierarchyKind.TIME,
                            hierarchy_role=HierarchyRole.TIME_SPAN, rollup=False, expand_depth=0,
                            disclosure=DisclosureLevel.L2, security=security)
        assert result.evidence_bundle.navigation["sources_added"] > 0
        assert materialization[0][0] == HOME
        assert len(materialization[0][1]) <= 4, "initial bodies loaded before candidate cap"
        limits = result.evidence_bundle.navigation
        assert limits["initial_candidates"] <= 4
        assert limits["source_population"] == 33
        assert limits["source_candidates"] <= limits["source_limit"] == 3
        assert limits["source_index_ids_returned"] <= 3
        assert limits["source_candidate_fraction"] <= 0.1
        assert limits["summary_candidate_fraction"] <= 0.25
        if adaptive:
            assert "detail_reference_index_hits" in limits
            assert limits["stages"][0]["budget"] == 256
        if adaptive:
            assert limits["stopping_reason"] in {"bounded_signals_saturated", "budget_limit"}
        else:
            assert limits["stopping_reason"] == "bounded_relation_plan"
        assert result.evidence_bundle.token_count <= result.evidence_bundle.token_budget

        class SummaryGuard:
            def __getattr__(self, name):
                return getattr(domain, name)

            def get(self, scope, ids):
                assert scope.session == "", "summary-only path read original source scope"
                return original_get(scope, ids)

        summary_nav = GraphEvidenceNavigator(SummaryGuard(), GraphNavigationPolicy(
            use_relations=False, include_original=False, max_reads=512,
        ))
        _, summary_bundle = summary_nav.pack_closure(
            HOME, query(), [ScoredMemoryUnit(root, 1)], EvidencePacker(len, "fixture"),
            max_tokens=12000, top_k=16,
        )
        assert summary_bundle.navigation["source_candidates"] == 0
    finally:
        runtime.close()
        Factory.reset_all()
