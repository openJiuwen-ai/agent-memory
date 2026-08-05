"""wikimem retained_eval compatibility tests."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from evaluation.wikimem import (
    CaseScore,
    EvalHarnessConfig,
    EvalOutput,
    ProgressUpdate,
    RetrievalCoverageSummary,
    RetrievedMemoryFile,
    StageProfileArtifact,
    StageProfiler,
    StageTimingRecord,
    build_eval_cases,
    build_question_profile,
    format_progress_message,
    parse_retrieval_plugin_list,
    parse_sample_filter,
    prepare_locomo_samples,
    run_python_locomo_retrieval_eval,
    run_retained_qmd_eval,
    summarize_scores,
    summarize_scores_by_locomo_category,
    write_harness_artifacts,
)
from evaluation.wikimem.retained_eval import (
    _extract_retrieved_evidence_ids,
    build_retained_memory_files,
)

pytestmark = pytest.mark.unit


def _sample_payload() -> dict:
    return {
        "sample_id": "conv-1",
        "conversation": {
            "session_2": [
                {
                    "speaker": "Alice",
                    "dia_id": "D2:1",
                    "text": "I adopted a dog.",
                    "blip_caption": "photo of dog",
                }
            ],
            "session_1": [
                {"speaker": "Bob", "dia_id": "D1:1", "text": "I like chess."}
            ],
            "session_1_date_time": "2026-01-01",
        },
        "qa": [
            {
                "question": "What game does Bob like?",
                "answer": "chess",
                "evidence": ["D1:1"],
                "category": 4,
            },
            {
                "question": "What animal did Alice adopt?",
                "answer": ["dog"],
                "evidence": ["D2:1"],
                "category": 2,
            },
        ],
        "session_summary": {"session_1": "Bob likes chess."},
        "event_summary": {
            "session_2": {"date": "2026-01-02", "Alice": ["Adopted a dog"]}
        },
        "observation": {"session_2": {"Alice": [{"evidence_id": "D2:1", "text": "dog"}]}},
    }


def test_prepare_locomo_samples_preserves_records_questions_and_notes() -> None:
    prepared = prepare_locomo_samples([_sample_payload()])

    assert len(prepared) == 1
    sample = prepared[0]
    assert sample.sample_id == "conv-1"
    assert [record.session_id for record in sample.records] == ["D1", "D2"]
    assert sample.records[1].text == "I adopted a dog. [caption: photo of dog]"
    assert sample.session_datetimes == {1: "2026-01-01"}
    assert sample.session_summaries == {1: "Bob likes chess."}
    assert sample.event_summaries[2].items_by_speaker == {"Alice": ["Adopted a dog"]}
    assert sample.observations[2][0].evidence_id == "D2:1"


def test_retained_builder_adds_rust_style_multimodal_memory_pages() -> None:
    sample = prepare_locomo_samples([_sample_payload()])[0]

    memory_pages = [
        file
        for file in build_retained_memory_files(sample, "/tmp/kb")
        if "/wiki/memories/" in file.file_path
    ]

    assert len(memory_pages) == 1
    assert memory_pages[0].file_path.endswith("D2_1_multimodal.md")
    assert "Evidence: D2:1" in memory_pages[0].content
    assert "photo of dog" in memory_pages[0].content


def test_prepare_locomo_samples_parses_rust_session_summary_keys() -> None:
    payload = _sample_payload()
    payload["session_summary"] = {"session_1_summary": "Bob likes chess."}

    sample = prepare_locomo_samples([payload])[0]

    assert sample.session_summaries == {1: "Bob likes chess."}


def test_prepare_locomo_samples_keeps_events_session_summaries_in_kb(tmp_path) -> None:
    payload = _sample_payload()
    payload["event_summary"] = {
        "events_session_2": {
            "date": "2026-01-02",
            "Alice": ["Alice adopted a dog after lunch."],
        }
    }

    sample = prepare_locomo_samples([payload])[0]
    files = build_retained_memory_files(sample, tmp_path / "workspace" / sample.sample_id)

    assert sample.event_summaries[2].date == "2026-01-02"
    event_file = next(
        file for file in files if file.file_path.endswith("/wiki/events/session_2_event_1.md")
    )
    assert "Alice adopted a dog after lunch." in event_file.content
    assert (
        "- [session_2 events](wiki/topics/session_2_events.md) - dated event index"
        in files[0].content
    )


def test_prepare_locomo_samples_keeps_session_observation_pairs_in_kb(tmp_path) -> None:
    payload = _sample_payload()
    payload["observation"] = {
        "session_2_observation": {
            "Alice": [["Alice adopted a dog after lunch.", "D2:1"]]
        }
    }

    sample = prepare_locomo_samples([payload])[0]
    files = build_retained_memory_files(sample, tmp_path / "workspace" / sample.sample_id)

    assert sample.observations[2][0].evidence_id == "D2:1"
    observations_file = next(
        file for file in files if file.file_path.endswith("/wiki/observations/D2_1_obs_1.md")
    )
    assert "- Evidence: D2:1" in observations_file.content
    assert "Alice adopted a dog after lunch." in observations_file.content


def test_build_retained_memory_files_writes_entity_pages_with_turn_links(tmp_path) -> None:
    sample = prepare_locomo_samples([_sample_payload()])[0]

    files = build_retained_memory_files(sample, tmp_path / "workspace" / sample.sample_id)

    entity_file = next(file for file in files if file.file_path.endswith("/wiki/entities/Alice.md"))
    assert "# Alice" in entity_file.content
    assert "[turn D2:1](../turns/D2_1.md)" in entity_file.content
    assert "- [profile](wiki/synthesis/profile.md)" in files[0].content


def test_build_retained_memory_files_caps_entity_turn_links_like_rust(tmp_path) -> None:
    payload = _sample_payload()
    for session_number in range(3, 15):
        payload["conversation"][f"session_{session_number}"] = [
            {
                "speaker": "Alice",
                "dia_id": f"D{session_number}:1",
                "text": f"Alice note {session_number} first.",
            },
            {
                "speaker": "Alice",
                "dia_id": f"D{session_number}:2",
                "text": f"Alice note {session_number} last.",
            },
        ]
    sample = prepare_locomo_samples([payload])[0]

    files = build_retained_memory_files(sample, tmp_path / "workspace" / sample.sample_id)

    entity = next(file for file in files if file.file_path.endswith("/wiki/entities/Alice.md"))
    assert entity.content.count("(../turns/") == 10


def test_prepare_locomo_samples_infers_evidence_id_when_turn_dia_id_is_missing() -> None:
    payload = _sample_payload()
    del payload["conversation"]["session_1"][0]["dia_id"]

    sample = prepare_locomo_samples([payload])[0]

    assert sample.records[0].dia_id == "D1:1"


def test_parse_sample_filter_accepts_numbers_commas_and_all() -> None:
    assert parse_sample_filter(None) is None
    assert parse_sample_filter("all") is None
    assert parse_sample_filter("1, conv-2 / custom; 3") == {"conv-1", "conv-2", "custom", "conv-3"}


def test_build_eval_cases_uses_one_based_case_ids_and_stringified_answer() -> None:
    sample = prepare_locomo_samples([_sample_payload()])[0]

    cases = build_eval_cases([sample], question_limit=1)

    assert len(cases) == 1
    assert cases[0].case_id == "conv-1::q1"
    assert cases[0].question_index == 0
    assert cases[0].answer == "chess"
    assert cases[0].expected_evidence == ["D1:1"]


def test_retained_retrieval_ranking_ignores_answer_and_evidence_labels(tmp_path) -> None:
    baseline_payload = _sample_payload()
    changed_payload = deepcopy(baseline_payload)
    changed_payload["qa"][0]["answer"] = "unrelated answer"
    changed_payload["qa"][0]["evidence"] = ["D2:1"]

    baseline = run_retained_qmd_eval(
        dataset_name="locomo",
        samples=prepare_locomo_samples([baseline_payload]),
        workspace_root=tmp_path / "baseline",
        top_k=4,
        question_limit=1,
    ).cases[0]
    changed = run_retained_qmd_eval(
        dataset_name="locomo",
        samples=prepare_locomo_samples([changed_payload]),
        workspace_root=tmp_path / "changed",
        top_k=4,
        question_limit=1,
    ).cases[0]

    assert changed.retrieved_evidence == baseline.retrieved_evidence
    assert [path.split("/conv-1/", 1)[-1] for path in changed.retrieved_file_paths] == [
        path.split("/conv-1/", 1)[-1] for path in baseline.retrieved_file_paths
    ]


def test_parse_retrieval_plugin_list_normalizes_and_drops_empty_tokens() -> None:
    assert parse_retrieval_plugin_list(" qmd-consensus, sparse_query ,, ama_anchor_consensus ") == [
        "qmd_consensus",
        "sparse_query",
        "ama_anchor_consensus",
    ]


def test_summarize_scores_matches_retained_eval_metric_rounding() -> None:
    scores = [
        _score("c1", expected=["e1", "e2"], retrieved=["e1"], hits=["e1"], category=4),
        _score("c2", expected=["e3"], retrieved=["e3", "x"], hits=["e3"], category=2),
    ]

    summary = summarize_scores("locomo", scores)
    categories = summarize_scores_by_locomo_category(scores)

    assert summary.total_cases == 2
    assert summary.cases_with_evidence == 2
    assert summary.evidence_precision_macro == 0.75
    assert summary.evidence_precision_micro == 0.6667
    assert summary.evidence_recall_macro == 0.75
    assert summary.evidence_recall_micro == 0.6667
    assert summary.full_evidence_hit_rate == 0.5
    assert [(item.category, item.label, item.evidence_recall_macro) for item in categories] == [
        (2, "2 Temporal", 1.0),
        (4, "4 Single Hop", 0.5),
    ]


def test_write_harness_artifacts_outputs_manifest_cases_and_stage_profile(tmp_path) -> None:
    score = _score(
        "conv-1::q1",
        expected=["D1:1"],
        retrieved=[],
        hits=[],
        category=4,
        coverage=RetrievalCoverageSummary(miss_stage="root"),
    )
    output = EvalOutput(
        summary=summarize_scores("locomo", [score]),
        cases=[score],
        stage_profile=StageProfileArtifact(
            created_at_ms=1,
            total_samples=1,
            total_cases=1,
            stages=[StageTimingRecord(stage="root", calls=1, total_ms=2.0, avg_ms=2.0, max_ms=2.0)],
        ),
    )
    config = EvalHarnessConfig(
        dataset_name="locomo",
        samples="conv-1",
        question_limit=1,
        top_k=24,
        workspace_root="/tmp/kb",
        llm_provider=None,
        retrieval_plugins=["qmd_consensus"],
    )

    write_harness_artifacts(tmp_path, output, config)

    assert (tmp_path / "run_manifest.json").exists()
    assert (tmp_path / "failure_report.md").exists()
    assert (tmp_path / "stage_profile.json").exists()
    assert (tmp_path / "cases" / "conv-1__q1.json").exists()
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["config"]["retrieval_plugins"] == ["qmd_consensus"]
    assert manifest["total_failures"] == 1


def test_stage_profiler_aggregates_and_sorts_stage_timings() -> None:
    profiler = StageProfiler()

    profiler.record_duration_ms("candidate_ranking", 2.34567)
    profiler.record_duration_ms("root_selector_retrieve", 6.0)
    profiler.record_duration_ms("candidate_ranking", 3.0)

    artifact = profiler.snapshot(total_samples=2, total_cases=5, created_at_ms=123)

    assert artifact.created_at_ms == 123
    assert artifact.total_samples == 2
    assert artifact.total_cases == 5
    assert artifact.stages == [
        StageTimingRecord(
            stage="root_selector_retrieve",
            calls=1,
            total_ms=6.0,
            avg_ms=6.0,
            max_ms=6.0,
        ),
        StageTimingRecord(
            stage="candidate_ranking",
            calls=2,
            total_ms=5.3457,
            avg_ms=2.6728,
            max_ms=3.0,
        ),
    ]


def test_run_retained_qmd_eval_retrieves_locomo_evidence_and_writes_artifacts(tmp_path) -> None:
    sample = prepare_locomo_samples([_sample_payload()])[0]

    output = run_retained_qmd_eval(
        dataset_name="locomo",
        samples=[sample],
        workspace_root=tmp_path / "workspace",
        top_k=4,
        question_limit=1,
        harness_root=tmp_path / "harness",
    )

    assert output.summary.total_cases == 1
    assert output.summary.evidence_recall_macro == 1.0
    assert output.summary.full_evidence_hit_rate == 1.0
    assert output.cases[0].hit_evidence == ["D1:1"]
    assert output.cases[0].retrieval_coverage is not None
    assert output.cases[0].retrieval_coverage.final_hit_evidence == ["D1:1"]
    assert (tmp_path / "harness" / "run_manifest.json").exists()
    assert (tmp_path / "harness" / "stage_profile.json").exists()


def test_extract_retrieved_evidence_ids_filters_unmatched_entity_turns() -> None:
    profile = build_question_profile("What game did Bob play after dinner?", ["Bob"])
    file = RetrievedMemoryFile(
        filename="bob.md",
        file_path="/sample/wiki/entities/bob.md",
        mtime_ms=1,
        content="\n".join(
            [
                "- [D1:1](../turns/d1_1.md) After dinner Bob played chess with Nora.",
                "- [D1:2](../turns/d1_2.md) Bob mentioned tennis during breakfast.",
            ]
        ),
    )

    assert _extract_retrieved_evidence_ids(file, "What game did Bob play after dinner?", profile) == [
        "D1:1"
    ]


def test_extract_retrieved_evidence_ids_keeps_family_entity_context() -> None:
    question = "What do Melanie's kids like?"
    profile = build_question_profile(question, ["Melanie"])
    file = RetrievedMemoryFile(
        filename="melanie.md",
        file_path="/sample/wiki/entities/melanie.md",
        mtime_ms=1,
        content="\n".join(
            [
                "- [D4:8](../turns/d4_8.md) Her kids like painting.",
                "- [D6:6](../turns/d6_6.md) Her children like piano.",
            ]
        ),
    )

    assert _extract_retrieved_evidence_ids(file, question, profile) == ["D4:8", "D6:6"]


def test_run_retained_qmd_eval_does_not_fill_top_k_with_unranked_source_roots(tmp_path) -> None:
    target_session = 52
    payload = {
        "sample_id": "conv-many",
        "conversation": {
            f"session_{index}": [
                {
                    "speaker": "User",
                    "dia_id": f"D{index}:1",
                    "text": (
                        "I graduated with a degree in Business Administration."
                        if index == target_session
                        else f"I talked about unrelated topic {index}."
                    ),
                }
            ]
            for index in range(1, 61)
        },
        "qa": [
            {
                "question": "What degree did I graduate with?",
                "answer": "Business Administration",
                "evidence": [f"D{target_session}:1"],
                "category": 4,
            }
        ],
        "session_summary": {
            f"session_{index}": (
                "The user graduated with a Business Administration degree."
                if index == target_session
                else f"Unrelated session {index}."
            )
            for index in range(1, 61)
        },
        "event_summary": {},
        "observation": {},
    }
    sample = prepare_locomo_samples([payload])[0]

    output = run_retained_qmd_eval(
        dataset_name="locomo",
        samples=[sample],
        workspace_root=tmp_path / "workspace",
        top_k=100,
        question_limit=1,
    )

    target_path = f"/wiki/sources/session_{target_session}.md"
    target_rank = next(
        index
        for index, path in enumerate(output.cases[0].retrieved_file_paths, start=1)
        if path.endswith(target_path)
    )
    assert target_rank <= 10
    assert output.cases[0].hit_evidence == [f"D{target_session}:1"]
    assert output.cases[0].retrieval_coverage is not None
    assert output.cases[0].retrieval_coverage.final_hit_evidence == [f"D{target_session}:1"]


def test_run_python_locomo_retrieval_eval_loads_dataset_and_writes_summary(tmp_path) -> None:
    dataset = tmp_path / "locomo.json"
    dataset.write_text(json.dumps([_sample_payload()]), encoding="utf-8")
    output_dir = tmp_path / "output"

    result = run_python_locomo_retrieval_eval(
        dataset_path=dataset,
        output_dir=output_dir,
        workspace_root=tmp_path / "workspace",
        top_k=4,
        question_limit=1,
    )

    assert result["summary"]["dataset_name"] == "locomo"
    assert result["summary"]["total_cases"] == 1
    assert result["summary"]["evidence_recall_macro"] == 1.0
    assert result["cases"][0]["hit_evidence"] == ["D1:1"]
    assert (output_dir / "locomo_retrieval_eval.json").exists()
    assert (output_dir / "harness" / "run_manifest.json").exists()


def test_stage_profile_artifact_merges_additional_stage_and_keeps_rust_sort() -> None:
    artifact = StageProfileArtifact(
        created_at_ms=123,
        total_samples=1,
        total_cases=2,
        stages=[
            StageTimingRecord(
                stage="candidate_ranking",
                calls=1,
                total_ms=5.0,
                avg_ms=5.0,
                max_ms=5.0,
            )
        ],
    )

    updated = artifact.with_additional_stage("root_selector_retrieve", 5.0)
    updated = updated.with_additional_stage("candidate_ranking", 2.25)

    assert updated.stages == [
        StageTimingRecord(
            stage="candidate_ranking",
            calls=2,
            total_ms=7.25,
            avg_ms=3.625,
            max_ms=5.0,
        ),
        StageTimingRecord(
            stage="root_selector_retrieve",
            calls=1,
            total_ms=5.0,
            avg_ms=5.0,
            max_ms=5.0,
        ),
    ]


def test_format_progress_message_matches_rust_shape() -> None:
    message = format_progress_message(
        ProgressUpdate(
            completed_cases=3,
            total_cases=9,
            sample_index=1,
            total_samples=4,
            sample_id="conv-1",
            question_index=2,
            sample_question_total=5,
        )
    )

    assert message == "sample 1/4 conv-1 q2/5 overall 3/9"


def _score(
    case_id: str,
    *,
    expected: list[str],
    retrieved: list[str],
    hits: list[str],
    category: int | None = None,
    coverage: RetrievalCoverageSummary | None = None,
) -> CaseScore:
    return CaseScore(
        case_id=case_id,
        sample_id=case_id.split("::", maxsplit=1)[0],
        question_index=0,
        question="question",
        category=category,
        expected_evidence=expected,
        retrieved_evidence=retrieved,
        hit_evidence=hits,
        retrieved_record_ids=[],
        retrieved_file_paths=[],
        retrieved_entrypoint_paths=[],
        knowledge_base_root="/tmp/kb",
        retrieved_record_count=0,
        retrieved_file_count=0,
        retrieved_entrypoint_count=0,
        evidence_precision=0.0 if not retrieved else len(hits) / len(retrieved),
        evidence_recall=0.0 if not expected else len(hits) / len(expected),
        full_evidence_hit=set(expected) <= set(hits),
        retrieval_coverage=coverage,
    )
