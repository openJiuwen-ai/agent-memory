"""Public add/evolve/search exercise the optional graph path without a new API."""


import pytest

from jiuwen_memory.api import Context, JobStatus, assemble_runtime
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.retrieval.types import DisclosureLevel
from tests.unit.api.fixtures import security_for
from tests.unit.api.hierarchy_api_fixtures import (
    HOME,
    runtime_config,
    task_options,
    write_snapshot,
)
from tests.unit.retrieval.test_graphmem_query_closure import response
from tests.unit.retrieval.test_query_parser import _RecordingLLM

pytestmark = pytest.mark.integration


def test_configured_personal_analysis_reaches_public_search_budget(tmp_path):
    """Factory wiring keeps named queries cheap and reserves analyzed query cost."""
    filename, _ = tokenizer_file(tmp_path)
    analysis = _RecordingLLM(response())

    @LlmProducer.register("graphmem_analysis_fixture")
    def build_model(config):
        return analysis

    config = runtime_config("in_memory")
    config["globals"]["graph_enabled"] = False
    config.setdefault("llm", {})["analysis"] = {"target": "graphmem_analysis_fixture"}
    config["query_parser"] = {"default": {"target": "graphmem_query", "params": {
        "llm": "analysis", "analysis_mode": "personal",
    }}}
    config["retriever"] = {"default": {"target": "pipeline", "params": {
        "query_parser": "default",
        "rerank_enabled": False,
        "evidence_packing": {"tokenizer_file": str(filename), "mode": "sources_first"},
        "graph_navigation": {"strategy": "relational", "semantic_source_terms": True,
                             "analysis_token_reserve": 128},
    }}}
    Factory.reset_all()
    runtime = assemble_runtime(config=config)
    try:
        api = runtime.api
        security = security_for(api, HOME)
        source = write_snapshot(api, 0)
        job = api.evolve(HOME, **vars(task_options()), security=security)
        assert api.job_status(job, security=security).status is JobStatus.SUCCEEDED
        for question, calls, reserve in (
            ("What snapshot evidence did Alice save?", 0, 0),
            ("What snapshot evidence did I save?", 1, 128),
        ):
            result = api.search(
                question, Context(HOME, extensions={"max_tokens": 2000}), top_k=8,
                hierarchy_kind=HierarchyKind.TIME, hierarchy_role=HierarchyRole.TIME_SPAN,
                disclosure=DisclosureLevel.L2, rollup=False, expand_depth=0, security=security,
            )
            assert not result.errors
            assert analysis.calls == calls
            bundle = result.evidence_bundle
            assert source.content in bundle.rendered_context
            assert bundle.navigation["requested_token_limit"] == 2000
            assert bundle.navigation["analysis_reserved_tokens"] == reserve
            assert bundle.navigation["effective_evidence_budget"] == 2000 - reserve
            assert bundle.navigation["evidence_graph"]["nodes"]
            assert "[Evidence graph]" in bundle.rendered_context
    finally:
        runtime.close()
        Factory.reset_all()


def tokenizer_file(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(tokenizers.models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    filename = tmp_path / "tokenizer.json"
    tokenizer.save(str(filename))
    return filename, tokenizer
