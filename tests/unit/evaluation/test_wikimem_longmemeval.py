"""LongMemEval example harness compatibility tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evaluation.wikimem import (
    ConversationRecord,
    LoCoMoQuestion,
    PreparedSample,
    SessionEvents,
    adapt_longmemeval_to_locomo_samples,
    load_longmemeval_samples,
    run_python_longmemeval_retrieval_eval,
    run_retained_qmd_eval,
)
from evaluation.wikimem.longmemeval import convert_retained_output_to_longmemeval
from evaluation.wikimem.retained_eval import build_retained_memory_files

pytestmark = pytest.mark.unit


def _longmemeval_rows() -> list[dict]:
    return [
        {
            "question_id": "q-1",
            "question_type": "single-session-user",
            "question": "What hobby do I enjoy?",
            "answer": "Chess",
            "question_date": "2024-01-03",
            "haystack_session_ids": ["sess-a", "sess-b"],
            "haystack_dates": ["2024-01-01", "2024-01-02"],
            "haystack_sessions": [
                [
                    {
                        "role": "user",
                        "content": "I really enjoy playing chess.",
                        "has_answer": True,
                    },
                    {"role": "assistant", "content": "That's a great hobby."},
                ],
                [
                    {"role": "user", "content": "I also went grocery shopping."},
                    {"role": "assistant", "content": "Nice."},
                ],
            ],
            "answer_session_ids": ["sess-a"],
        }
    ]


def test_load_longmemeval_samples_maps_session_and_turn_targets(tmp_path) -> None:
    dataset = tmp_path / "longmemeval.json"
    dataset.write_text(json.dumps(_longmemeval_rows()), encoding="utf-8")

    samples = load_longmemeval_samples(dataset)

    assert len(samples) == 1
    assert samples[0]["question_id"] == "q-1"
    assert samples[0]["answer_session_ids"] == ["sess-a"]
    assert samples[0]["answer_turn_ids"] == ["sess-a_1"]
    assert samples[0]["session_documents"][0]["corpus_id"] == "sess-a"
    assert samples[0]["turn_documents"][0]["corpus_id"] == "sess-a_1"


def test_load_longmemeval_samples_uses_raw_turn_index_for_answer_turn_ids(tmp_path) -> None:
    rows = _longmemeval_rows()
    rows[0]["haystack_sessions"][0] = [
        {"role": "assistant", "content": "Preface."},
        {"role": "user", "content": "I enjoy chess.", "has_answer": True},
    ]
    dataset = tmp_path / "longmemeval.json"
    dataset.write_text(json.dumps(rows), encoding="utf-8")

    samples = load_longmemeval_samples(dataset)

    assert samples[0]["answer_turn_ids"] == ["sess-a_2"]
    assert samples[0]["_locomo_evidence_by_turn_id"]["sess-a_2"] == "D1:1"


def test_run_python_longmemeval_retrieval_eval_writes_summary(tmp_path) -> None:
    dataset = tmp_path / "longmemeval.json"
    dataset.write_text(json.dumps(_longmemeval_rows()), encoding="utf-8")
    output_dir = tmp_path / "output"

    result = run_python_longmemeval_retrieval_eval(
        dataset_path=dataset,
        output_dir=output_dir,
        workspace_root=tmp_path / "workspace",
        top_k=4,
        granularity="turn",
    )

    assert result["summary"]["dataset_name"] == "longmemeval"
    assert result["summary"]["total_cases"] == 1
    assert result["summary"]["averaged_metrics"]["turn"]["recall_any@1"] == 1.0
    assert result["summary"]["averaged_metrics"]["turn"]["recall_all@1"] == 1.0
    assert result["summary"]["averaged_metrics"]["turn"]["ndcg_any@1"] == 1.0
    assert result["cases"][0]["retrieval_results"]["metrics"]["turn"]["recall_any@1"] == 1.0
    assert (output_dir / "longmemeval_retrieval_eval.json").exists()
    assert (output_dir / "harness" / "run_manifest.json").exists()


def test_run_python_longmemeval_retrieval_eval_skips_turn_cases_without_answer_turns(
    tmp_path,
) -> None:
    rows = _longmemeval_rows()
    rows[0]["question_id"] = "q-no-turn-target"
    rows[0]["haystack_sessions"][0][0]["has_answer"] = False
    dataset = tmp_path / "longmemeval.json"
    dataset.write_text(json.dumps(rows), encoding="utf-8")
    output_dir = tmp_path / "output"

    result = run_python_longmemeval_retrieval_eval(
        dataset_path=dataset,
        output_dir=output_dir,
        workspace_root=tmp_path / "workspace",
        top_k=4,
        granularity="turn",
    )

    assert result["summary"]["evaluated_cases"] == 0
    assert result["summary"]["skipped_no_target_cases"] == 1
    assert result["summary"]["averaged_metrics"] == {"turn": {}, "session": {}}
    assert result["cases"][0]["evaluated"] is False
    assert result["cases"][0]["retrieval_results"]["metrics"] == {"turn": {}, "session": {}}


def test_longmemeval_adapter_builds_rust_fallback_multiview_files(tmp_path) -> None:
    samples = adapt_longmemeval_to_locomo_samples(_longmemeval_rows())
    sample = samples[0]

    assert sample.observations[1][0].evidence_id == "D1:1"
    assert sample.event_summaries[1].items_by_speaker["User"] == [
        "Evidence D1:1: I really enjoy playing chess."
    ]
    paths = [
        file.file_path
        for file in build_retained_memory_files(sample, tmp_path / "workspace")
    ]
    assert any("/wiki/observations/" in path for path in paths)
    assert any("/wiki/events/" in path for path in paths)
    assert any("/wiki/entities/" in path for path in paths)


def test_longmemeval_metrics_count_ranked_page_support_like_rust(tmp_path) -> None:
    rows = _longmemeval_rows()
    rows[0]["haystack_sessions"][0].insert(
        1,
        {"role": "user", "content": "I also enjoy go.", "has_answer": True},
    )
    dataset = tmp_path / "longmemeval.json"
    dataset.write_text(json.dumps(rows), encoding="utf-8")
    samples = load_longmemeval_samples(dataset)
    adapt_longmemeval_to_locomo_samples(samples)
    score = SimpleNamespace(
        sample_id="q-1",
        question=rows[0]["question"],
        knowledge_base_root="workspace/q-1",
        retrieved_file_paths=["workspace/q-1/wiki/sources/session_1.md"],
        retrieved_evidence=[],
    )

    result = convert_retained_output_to_longmemeval(
        samples=samples,
        retained_cases=[score],
        granularity="turn",
        workspace_root="workspace",
    )

    case = result["cases"][0]
    assert case.retrieval_results.metrics["turn"]["recall_all@1"] == 1.0


def test_run_retained_qmd_eval_uses_multiview_profile_for_longmemeval(tmp_path) -> None:
    sample = PreparedSample(
        sample_id="long-1",
        raw_sample={},
        records=[
            ConversationRecord(
                dia_id="D1:1",
                session_id="D1",
                speaker="User",
                text="The answer token is azure-harbor.",
            )
        ],
        questions=[
            LoCoMoQuestion(
                question="Which token did I mention?",
                answer="azure-harbor",
                evidence=["D1:1"],
            )
        ],
        session_datetimes={1: "2024-01-01"},
        session_summaries={1: "The answer token is azure-harbor."},
        event_summaries={
            1: SessionEvents(
                date="2024-01-01",
                items_by_speaker={"User": ["The answer token is azure-harbor."]},
            )
        },
        observations={},
    )

    output = run_retained_qmd_eval(
        dataset_name="longmemeval",
        samples=[sample],
        workspace_root=tmp_path / "workspace",
        top_k=8,
    )

    paths = output.cases[0].retrieved_file_paths
    assert any("/wiki/events/" in path or "/wiki/entities/" in path for path in paths)
