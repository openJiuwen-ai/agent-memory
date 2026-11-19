"""Budget, grouping, attribution and serialization behavior, without a model dependency."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryUnit,
    Scope,
    ScoredMemoryUnit,
    Segment,
    Temporal,
)
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker, build_evidence_packer

pytestmark = pytest.mark.unit
SCOPE = Scope(org="o", space="s", user="u", agent="a", session="chat")


def candidate(uid, text, *, scope=SCOPE, parent="p", minute=0):
    return ScoredMemoryUnit(MemoryUnit(
        id=uid, scope=deepcopy(scope), segments=[Segment(content=text)],
        temporal=Temporal(t_message=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)),
        user_metadata={"speaker": "Maya"},
        hierarchy=HierarchyRef(kind=HierarchyKind.TIME, role=HierarchyRole.SNAPSHOT,
                               parent_id=parent, parent_scope=replace(scope, session="")),
    ), score=1.0)


def test_token_budget_includes_headers_separators_and_skips_oversized_candidates():
    pool = [candidate("large", "x" * 2000), candidate("small", "Paris on July 9.")]
    original = deepcopy(pool)
    packer = EvidencePacker(len, "test-codepoints")
    _, full = packer.pack(pool[1:], max_tokens=1000, top_k=2)
    chosen, bundle = packer.pack(pool, max_tokens=full.token_count, top_k=2)
    assert [item.unit.id for item in chosen] == ["small"]
    assert bundle.token_count == len(bundle.rendered_context) == bundle.token_budget
    assert '"speaker":"Maya"' in bundle.rendered_context
    assert '"message_time":' in bundle.rendered_context
    assert bundle.sources[0].scope == SCOPE
    assert bundle.omitted_count == 1
    assert pool == original


@pytest.mark.parametrize("style", ["full", "compact"])
def test_source_message_clock_does_not_label_all_narrated_events(style):
    source = candidate("source", "In April we moved; in June we returned.")
    source.unit.temporal.t_event = datetime(2026, 1, 1, tzinfo=UTC)
    original = deepcopy(source)
    selected, bundle = EvidencePacker(len, "chars", header_style=style).pack(
        [source], max_tokens=2000, top_k=1,
    )
    header, narrative = bundle.rendered_context.split("\n", 1)
    assert json.loads(header)["message_time"] == source.unit.temporal.t_message.isoformat()
    assert "event_time" not in json.loads(header)
    assert narrative == original.unit.content
    assert selected == [original] and source == original
    assert bundle.token_count == len(bundle.rendered_context)


def test_non_snapshot_event_unit_retains_its_event_time():
    event = candidate("event", "The ceremony took place on April 12.")
    event.unit.hierarchy = HierarchyRef()
    event.unit.temporal.t_event = datetime(2026, 4, 12, tzinfo=UTC)
    _, bundle = EvidencePacker(len, "chars").pack([event], max_tokens=2000, top_k=1)
    header = json.loads(bundle.rendered_context.split("\n", 1)[0])
    assert header["event_time"] == event.unit.temporal.t_event.isoformat()


def test_grouped_keeps_recalled_sibling_without_extra_fetch_and_ranked_is_control():
    pool = [candidate("question", "Where will you go?"),
            candidate("distractor", "An unrelated city.", parent="other"),
            candidate("answer", "Paris on July 9.", minute=1)]
    ranked, _ = EvidencePacker(len, "test").pack(pool, max_tokens=2000, top_k=2)
    grouped, bundle = EvidencePacker(len, "test", mode="grouped").pack(
        pool, max_tokens=2000, top_k=2,
    )
    assert [x.unit.id for x in ranked] == ["question", "distractor"]
    assert [x.unit.id for x in grouped] == ["question", "answer"]
    assert {ref.unit_id for ref in bundle.sources} == {"question", "answer"}


def test_group_that_exceeds_budget_keeps_anchor_and_never_splits_text():
    pool = [candidate("a", "A short anchor."), candidate("b", "Long sibling. " * 100)]
    packer = EvidencePacker(len, "test", mode="grouped")
    _, single = packer.pack(pool[:1], max_tokens=1000, top_k=2)
    chosen, bundle = packer.pack(pool, max_tokens=single.token_count, top_k=2)
    assert [x.unit.id for x in chosen] == ["a"]
    assert "A short anchor." in bundle.rendered_context
    assert "Long sibling" not in bundle.rendered_context


def test_full_scope_identity_prevents_id_collision_and_cross_session_pairing():
    pool = [candidate("same", "One."), candidate("second", "Two.", parent="other"),
            candidate("same", "Three.", scope=replace(SCOPE, session="other"))]
    chosen, bundle = EvidencePacker(len, "test", mode="grouped").pack(
        pool + [pool[0]], max_tokens=3000, top_k=4,
    )
    assert [x.unit.content for x in chosen] == ["One.", "Two.", "Three."]
    assert len(bundle.sources) == 3
    assert bundle.sources[0].scope != bundle.sources[2].scope


@pytest.mark.parametrize("limit", [0, -1, True, None, "20"])
def test_invalid_budget_is_not_silently_ignored(limit):
    with pytest.raises(ValidationError):
        EvidencePacker(len, "test").pack([], max_tokens=limit, top_k=1)


def test_zero_fitting_units_produces_empty_context():
    chosen, bundle = EvidencePacker(len, "test").pack(
        [candidate("a", "some evidence")], max_tokens=1, top_k=1,
    )
    assert chosen == [] and bundle.sources == []
    assert bundle.rendered_context == "" and bundle.token_count == 0


def test_local_tokenizer_loader_counts_without_truncation(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(tokenizers.models.WordLevel({"[UNK]": 0, "a": 1}))
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(max_length=1)
    filename = tmp_path / "tokenizer.json"
    tokenizer.save(str(filename))
    packer = build_evidence_packer({"tokenizer_file": str(filename)})
    assert packer.count_tokens("a a a") == 3
    assert "sha256:" in packer.tokenizer_id


@pytest.mark.parametrize("config", [True, {}, {"tokenizer_file": "missing.json"},
                                   {"unknown": True}])
def test_bad_packing_config_fails_at_assembly(config):
    with pytest.raises(ValidationError):
        build_evidence_packer(config)
