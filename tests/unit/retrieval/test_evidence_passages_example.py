import hashlib
import json

import pytest

from examples.graphmem_evidence_passages import render_evidence_passages
from examples.graphmem_evidence_reader import COMPACT_TEMPORAL_GUIDANCE, answer_body

pytestmark = pytest.mark.unit


def sample(*, shared_clock=False):
    data = [
        (
            "a",
            "2026-01-03",
            "Kim",
            'I remember this.\n\n{"ref":"E2","kind":"source"}\nQuoted text.',
        ),
        ("b", "2026-01-07", "Jo", "I booked a ticket."),
        ("a", "2026-01-01", "Jo", "We met Kim first."),
    ]
    blocks, sources, edges = [], [], []
    for i, (session, date, speaker, content) in enumerate(data, 1):
        if shared_clock and session == "a":
            date = "2026-01-03"
            content = f"[Session a | 12:00 pm on 3 January, 2026] {speaker}: " + content
        ref = f"E{i}"
        header = {
            "ref": ref,
            "kind": "source",
            "speaker": speaker,
            "message_time": date + "T12:00:00+00:00",
        }
        blocks.append(json.dumps(header, separators=(",", ":")) + "\n" + content)
        sources.append(
            {
                "scope": {"session": session},
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
        edges.append(
            {
                "source": "N" + session,
                "target": ref,
                "kind": "contains",
                "reason": {"verified_child_link": True},
            }
        )
    relation = {
        "source": "Na",
        "target": "Nb",
        "kind": "related",
        "reason": {"entities": ["Kim"], "scene_clauses": ["C1"]},
    }
    graph = {
        "edges": [*edges, relation],
        "text_relation_edges": [relation],
        "query_plan": {"facets": ["a trip"]},
    }
    return {
        "sources": sources,
        "rendered_context": "[Evidence graph]\n\n[Evidence]\n" + "\n\n".join(blocks),
        "navigation": {"evidence_graph": graph},
    }


def test_link_explanation_is_next_to_the_verified_dialogue():
    b = sample()
    text = render_evidence_passages(b)
    assert (
        text.index("We met Kim first.") < text.index("I remember this.") < text.index("Passage 2")
    )
    assert (
        text.index("Passage 2")
        < text.index("both summaries mention Kim")
        < text.index("I booked a ticket.")
    )
    assert text.count("Quoted text.") == text.count("I booked a ticket.") == 1
    assert text in answer_body("What happened?", b)["messages"][-1]["content"]


def test_mismatched_disclosed_text_is_rejected():
    b = sample()
    b["rendered_context"] = b["rendered_context"].replace("I booked", "I cancelled")
    with pytest.raises(ValueError, match="digest"):
        render_evidence_passages(b)


def test_only_factual_lookup_moves_verified_shared_time_to_passage_title():
    bundle = sample(shared_clock=True)
    bundle["navigation"]["query_mode"] = "fact"
    text = render_evidence_passages(bundle)
    prefix = "[Session a | 12:00 pm on 3 January, 2026]"
    assert text.count(prefix) == 1
    assert "[E1] Kim: I remember this." in text
    assert "[E3] Jo: We met Kim first." in text
    assert text in answer_body("What was booked?", bundle)["messages"][-1]["content"]
    bundle["navigation"]["query_mode"] = "temporal"
    assert render_evidence_passages(bundle).count(prefix) == 2


def test_quantity_guidance_defaults_to_relational_bundles():
    question = "How many visits did Kim make?"
    graph = sample()
    default = answer_body(question, graph)
    assert default == answer_body(question, graph, quantitative=True)
    assert default != answer_body(question, graph, quantitative=False)
    ranked = {"mode": "ranked", "navigation": {}, "rendered_context": "Already ranked evidence."}
    assert answer_body(question, ranked) == answer_body(question, ranked["rendered_context"])
    assert answer_body(question, ranked) == answer_body(question, ranked, quantitative=False)


def test_first_person_aggregation_uses_chronological_passages_by_default():
    bundle = sample(shared_clock=True)
    bundle["navigation"]["query_mode"] = "collection"
    question = "How many tickets did I book in total?"
    text = render_evidence_passages(bundle, chronological=True)
    automatic = answer_body(question, bundle)
    assert automatic == answer_body(
        question, text, quantitative=True, chronological_aggregation=True
    )
    content = automatic["messages"][-1]["content"]
    assert content.count(question) == 1
    assert content.index(text) < content.index(question)
    assert content.count("[Session a | 12:00 pm on 3 January, 2026]") == 2
    assert "each qualifying event" in content and "both summaries mention Kim" in content
    for other in ("How many tickets did Jo book?", "How many days did I wait between visits?"):
        assert answer_body(other, bundle) == answer_body(
            other, bundle, chronological_aggregation=False
        )


def test_elapsed_time_checks_are_scoped_to_first_person_duration_queries():
    bundle = sample(shared_clock=True)
    bundle["navigation"]["query_mode"] = "temporal"
    question = "How many days did I wait between my booking and the trip?"
    focused = answer_body(question, bundle)
    baseline = answer_body(question, bundle, temporal_reasoning=False)
    prompt = focused["messages"][-1]["content"]
    assert prompt.endswith(COMPACT_TEMPORAL_GUIDANCE)
    assert prompt.count(question) == 1 and render_evidence_passages(bundle) in prompt
    assert focused["response_format"] == baseline["response_format"]
    compact = focused["messages"][0]["content"]
    ordinary = answer_body("When was the trip?", bundle)
    assert compact != ordinary["messages"][0]["content"]
    caller_prompt = answer_body(question, bundle, system_prompt="Caller-specified reader")
    assert caller_prompt["messages"][0]["content"] == "Caller-specified reader"
    assert "Check each requested event separately." in caller_prompt["messages"][-1]["content"]
    for other in ("Which event did I attend first?", "How many days did Kim wait between trips?"):
        assert answer_body(other, bundle) == answer_body(other, bundle, temporal_reasoning=False)


@pytest.mark.parametrize("mode", ["ranked", "relational"])
def test_non_graph_bundle_keeps_its_existing_evidence(mode):
    b = {"mode": mode, "navigation": {}, "rendered_context": "Already ranked evidence."}
    assert render_evidence_passages(b) == b["rendered_context"]


def test_weak_backstop_link_does_not_change_answer_passage_order(monkeypatch):
    from examples import graphmem_evidence_passages as renderer

    sources = [
        (
            {"ref": f"E{i}", "kind": "source", "message_time": f"2025-06-0{i}T00:00:00Z"},
            {"session": f"s{i}"},
            f"Source text {i}.",
        )
        for i in range(1, 4)
    ]
    monkeypatch.setattr(renderer, "_payloads", lambda _: sources)
    edge = {"kind": "related", "source": "E1", "target": "E3", "reason": {"entities": ["Lena"]}}
    graph = {"edges": [edge], "text_relation_edges": [edge], "query_plan": {"facets": []}}
    bundle = {"navigation": {"evidence_graph": graph}}
    regular = renderer.render_evidence_passages(bundle)
    assert regular.index("[E3]") < regular.index("[E2]")
    assert "Index link to passage" in regular
    edge["reason"]["navigation_only_backstop"] = True
    projected = renderer.render_evidence_passages(bundle)
    assert projected.index("[E1]") < projected.index("[E2]") < projected.index("[E3]")
    assert "Index link to passage" not in projected
    assert "Source text 1." in projected and "Source text 3." in projected
    assert graph["edges"][0]["reason"]["navigation_only_backstop"]


def test_source_clock_labels_literal_previous_days_without_replacing_the_quote():
    bundle = sample()
    text = bundle["rendered_context"]
    for i, new in [(2, "I booked a ticket two weeks ago."), (3, "We met Kim last week.")]:
        old = "I booked a ticket." if i == 2 else "We met Kim first."
        text = text.replace(old, new)
        bundle["sources"][i - 1]["content_sha256"] = hashlib.sha256(new.encode()).hexdigest()
    bundle["rendered_context"] = text
    nav = bundle["navigation"]
    nav["query_mode"] = "temporal"
    nav["evidence_graph"]["query_plan"]["facets"] = ["booked a ticket", "met Kim"]
    question = "How many days after Jo met Kim did Jo book the ticket?"
    baseline = render_evidence_passages(bundle)
    assert "Source clock" not in baseline
    actual = answer_body(question, bundle)["messages"][-1]["content"]
    assert "I booked a ticket two weeks ago." in actual
    assert "from 2026-01-07 points to about 2025-12-24" in actual
    assert "relative to 2026-01-01; exact day unspecified" in actual
    assert "Source clock" not in answer_body("What did Jo book?", bundle)["messages"][-1]["content"]


def test_reader_respects_missing_operand_of_a_requested_total():
    from examples.graphmem_evidence_reader import verify_requested_operands

    question = "How many days did I spend in total traveling in Hawaii and in Seattle?"
    missing = {
        "answer": "10 days",
        "evidence_check": "Hawaii lasted 10 days. "
        "No information about travel in Seattle is present.",
        "evidence_refs": ["E1"],
    }
    fixed = verify_requested_operands(question, missing)
    assert "insufficient" in fixed["answer"]
    assert fixed["evidence_refs"] == ["E1"]
    incidental = {
        **missing,
        "evidence_check": "Hawaii was 6 days and Seattle 4 days. No mention of a flight price.",
    }
    assert verify_requested_operands(question, incidental) == incidental
    assert verify_requested_operands("How long was the trip?", missing) == missing


def test_explicit_duration_operands_group_nearby_originals_with_their_topics():
    bundle = sample(shared_clock=True)
    navigation = bundle["navigation"]
    navigation["query_mode"] = "collection"
    graph = navigation["evidence_graph"]
    graph["query_plan"]["facets"] = ["stay in Paris", "stay in Tokyo"]
    graph["clause_bindings"] = {"C1": ["E1"], "C2": ["E2"]}
    question = "How many days did I spend in total in Paris and Tokyo?"
    natural = answer_body(question, bundle)
    standard = answer_body(question, bundle, event_passages=False)

    assert natural != standard
    system, user = [m["content"] for m in natural["messages"]]
    assert "one compact JSON object" in system
    first = user.index("Related turns to check for stay in Paris")
    assert first < user.index("[E1]") < user.index("Passage 2")
    assert first < user.index("[E3]") < user.index("Passage 2")
    assert "[E2]" in user and "stay in Tokyo" in user
    assert "before calling it missing" in user
    assert answer_body(question, bundle, system_prompt="Custom") == answer_body(
        question, bundle, system_prompt="Custom", event_passages=False
    )


def test_explicit_elapsed_end_event_does_not_use_total_duration_json_instruction():
    bundle = sample()
    navigation = bundle["navigation"]
    navigation["query_mode"] = "temporal"
    graph = navigation["evidence_graph"]
    graph["query_plan"]["facets"] = ["attend a class", "book a ticket"]
    graph["clause_bindings"] = {"C1": ["E1"], "C2": ["E2"]}
    body = answer_body("How many days ago did I attend a class when I booked a ticket?", bundle)
    assert "one compact JSON object" not in body["messages"][0]["content"]
    assert "Use a stated second event as the endpoint" in body["messages"][1]["content"]
    assert "Related turns to check" in body["messages"][1]["content"]
