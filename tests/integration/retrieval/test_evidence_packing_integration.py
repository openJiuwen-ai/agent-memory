"""Opt-in packing through all storage pipelines and the authenticated public API."""

from dataclasses import replace

import pytest

from jiuwen_memory.api import Context, assemble_runtime
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import LifecycleState, RetrievalPipeline, Scope
from jiuwen_memory.retrieval.cross_space import merge
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker, EvidenceRetrievalResult
from jiuwen_memory.retrieval.types import DisclosureLevel, RecallChannel, RetrievalQuery
from jiuwen_memory_entry.core.api_contract import to_jsonable
from tests.unit.api.fixtures import runtime_config, security_for
from tests.unit.retrieval.test_evidence_packing import candidate
from tests.unit.retrieval.test_storage_pipelines import IdRecaller, _build_retriever

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_three_pipelines_keep_visibility_checks_and_opt_in_result(pipeline):
    retriever, domain, _kv, scope = _build_retriever(
        pipeline, [IdRecaller(RecallChannel.KEYWORD, "u1"),
                   IdRecaller(RecallChannel.VECTOR, "deleted")],
    )
    forgotten = candidate("deleted", "MUST NOT LEAK", scope=scope).unit
    forgotten.lifecycle = LifecycleState.FORGOTTEN
    domain.add(scope, [forgotten])
    query = RetrievalQuery(text="one", top_k=4, disclosure=DisclosureLevel.L2,
                           max_tokens=1000, with_trajectory=True)
    baseline = retriever.retrieve(scope, query)
    assert "evidence_bundle" not in to_jsonable(baseline)
    retriever._evidence_packer = EvidencePacker(len, "test-codepoints")
    result = retriever.retrieve(scope, query)
    assert isinstance(result, EvidenceRetrievalResult)
    assert [item.unit_id for item in result.items] == ["u1"]
    assert "MUST NOT LEAK" not in result.evidence_bundle.rendered_context
    assert result.evidence_bundle.token_count <= query.max_tokens
    assert any(step.stage == "evidence_pack" for step in result.trajectory)
    serialized = to_jsonable(result)
    assert serialized["evidence_bundle"]["sources"][0]["scope"]["org"] == scope.org
    with pytest.raises(ValidationError):
        retriever.retrieve(scope, replace(query, max_tokens=None))


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
def test_public_api_factory_roundtrip_and_default_compatibility(tmp_path, engine):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordLevel({"[UNK]": 0, "Paris": 1}, unk_token="[UNK]"),
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    filename = tmp_path / "tokenizer.json"
    tokenizer.save(str(filename))
    scope = Scope(org="test", user="alice", agent="assistant", session="conversation")
    for enabled in (False, True):
        Factory.reset_all()
        config = runtime_config(engine)
        if enabled:
            config["retriever"] = {"default": {"target": "pipeline", "params": {
                "rerank_enabled": False,
                "evidence_packing": {"mode": "grouped", "tokenizer_file": str(filename)},
            }}}
        runtime = assemble_runtime(config=config)
        try:
            api = runtime.api
            security = security_for(api, scope)
            api.add("Maya will travel to Paris on July 9.", scope, security=security,
                    system_metadata={"infer": False})
            result = api.search(
                "Paris", Context(scope, extensions={"max_tokens": 1000}),
                disclosure=DisclosureLevel.L2, expand_depth=0, rollup=False,
                security=security, with_trajectory=True,
            )
            assert result.items
            payload = to_jsonable(result)
            assert ("evidence_bundle" in payload) is enabled
            if enabled:
                bundle = result.evidence_bundle
                assert "Paris" in bundle.rendered_context
                assert bundle.token_count == len(tokenizer.encode(bundle.rendered_context).ids)
                empty = api.search(
                    " ", Context(scope, extensions={"max_tokens": 1000}),
                    disclosure=DisclosureLevel.L2, expand_depth=0, rollup=False,
                    security=security,
                )
                assert isinstance(empty, EvidenceRetrievalResult)
                assert empty.evidence_bundle.rendered_context == ""
        finally:
            runtime.close()
            Factory.reset_all()


def test_cross_space_does_not_silently_discard_a_counted_bundle():
    _, bundle = EvidencePacker(len, "test").pack([], max_tokens=500, top_k=2)
    result = EvidenceRetrievalResult(evidence_bundle=bundle)
    assert merge([("one", result)], top_k=2) is result
    with pytest.raises(ValidationError, match="cross-space"):
        merge([("one", result), ("two", result)], top_k=2)
