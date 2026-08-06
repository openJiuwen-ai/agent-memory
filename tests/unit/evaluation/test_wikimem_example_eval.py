"""Example dataset runner tests for wikimem migration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evaluation.wikimem import example_eval
from evaluation.wikimem.example_eval import (
    build_mem_gallery_python_workspaces,
    build_meta_crag_python_workspaces,
    run_evermembench_python_eval,
    run_filesystem_proxy_eval,
    run_locomo_refined_offline_eval,
)
from evaluation.wikimem.qmd_consensus import RetrievedMemoryFile

pytestmark = pytest.mark.unit


def _mark_python_workspace(root, dataset_name: str) -> None:
    (root / ".wikimem-workspace.json").write_text(
        json.dumps(
            {
                "producer": "mem2.0.wikimem.python",
                "dataset_name": dataset_name,
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )


def test_build_mem_gallery_python_workspaces_writes_clean_multimodal_wiki(tmp_path) -> None:
    dialog_root = tmp_path / "dialog"
    dialog_root.mkdir()
    (dialog_root / "Robot_Art.json").write_text(
        json.dumps(
            {
                "character_profile": {"name": "Ada"},
                "multi_session_dialogues": [
                    {
                        "session_id": "D6",
                        "date": "2026-01-02",
                        "dialogues": [
                            {
                                "round": "D6:1",
                                "user": "I set up a robot artist easel.",
                                "assistant": "The easel has a cobalt palette.",
                                "input_image": ["../image/Robot_Art/d6.png"],
                                "image_caption": ["robot artist with cobalt palette"],
                                "image_id": ["IMG_7"],
                            }
                        ],
                    }
                ],
                "human-annotated QAs": [
                    {
                        "question": "Which palette was on the robot artist easel?",
                        "answer": "cobalt",
                        "point": "visual",
                        "clue": ["D6:1"],
                        "session_id": ["D6"],
                        "question_image": "../image/Robot_Art/q.png",
                        "image_caption": "robot artist easel",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cases = build_mem_gallery_python_workspaces(
        dialog_root=dialog_root,
        workspace_root=tmp_path / "workspace",
    )

    assert cases == [
        {
            "case_id": "Robot_Art::q1",
            "question": "Which palette was on the robot artist easel?",
            "silver_evidence_ids": ["D6:1"],
            "knowledge_base_root": str(tmp_path / "workspace" / "Robot_Art"),
            "question_image_caption": "robot artist easel",
            "source_session_ids": ["D6"],
        }
    ]
    kb = tmp_path / "workspace" / "Robot_Art"
    assert "Image caption: robot artist with cobalt palette" in (
        kb / "raw" / "turns" / "DD6_1.md"
    ).read_text(encoding="utf-8")
    assert "Evidence: D6:1" in (
        kb / "wiki" / "turns" / "D6_1.md"
    ).read_text(encoding="utf-8")
    assert json.loads((kb / ".mem-gallery" / "artifact_support_map.json").read_text(encoding="utf-8")) == [
        {
            "memory_path": (kb / "wiki" / "memories" / "clue-summary-D6_1.md").as_posix(),
            "linked_clue_ids": ["D6:1"],
        }
    ]
    retrieval = json.loads(
        (kb / ".kb-research" / "retrieval" / "D6_1.json").read_text(encoding="utf-8")
    )
    assert retrieval["evidence_id"] == "D6:1"
    assert "robot artist with cobalt palette" in retrieval["searchable_text"]
    provenance = json.loads((kb / ".wikimem-workspace.json").read_text(encoding="utf-8"))
    assert provenance["producer"] == "mem2.0.wikimem.python"


def test_build_meta_crag_python_workspaces_labels_without_rust_cases(tmp_path) -> None:
    data_root = tmp_path / "meta"
    data_root.mkdir()
    (data_root / "validation.json").write_text(
        json.dumps(
            [
                {
                    "session_id": "s1",
                    "image_url": "https://example.test/city.png",
                    "turns": {
                        "query": ["I visited Paris.", "Which city did I visit?"],
                        "search_query": ["visited Paris", "visited city"],
                        "answer": ["Paris", ""],
                    },
                    "answers": {"ans_full": ["Paris", "Paris"]},
                }
            ]
        ),
        encoding="utf-8",
    )

    cases = build_meta_crag_python_workspaces(
        data_root=data_root,
        workspace_root=tmp_path / "workspace",
        dataset_variant="single_turn",
    )

    q2 = cases[1]
    assert q2["case_id"] == "s1::q2"
    assert q2["silver_evidence_ids"] == ["turn-0"]
    kb = tmp_path / "workspace" / "s1__q2"
    assert "Evidence: turn-0" in (kb / "wiki" / "memories" / "turn-0.md").read_text(encoding="utf-8")
    retrieval = json.loads((kb / ".kb-research" / "retrieval" / "turn-0.json").read_text(encoding="utf-8"))
    assert retrieval["evidence_id"] == "turn-0"
    assert "Paris" in retrieval["searchable_text"]


def test_run_locomo_refined_offline_eval_reuses_retained_runner(tmp_path) -> None:
    dataset = tmp_path / "locomo_refined.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "sample_id": "conv-x",
                    "conversation": {
                        "session_1": [
                            {
                                "speaker": "Ana",
                                "dia_id": "D1:1",
                                "text": "I painted a blue lighthouse.",
                                "blip_caption": "blue lighthouse painting",
                            }
                        ]
                    },
                    "qa": [
                        {
                            "question": "What did Ana paint?",
                            "answer": "blue lighthouse",
                            "evidence": ["D1:1"],
                            "category": 4,
                        }
                    ],
                    "session_summary": {"session_1": "Ana painted a blue lighthouse."},
                    "event_summary": {},
                    "observation": {},
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_locomo_refined_offline_eval(
        dataset_path=dataset,
        output_dir=tmp_path / "out",
        workspace_root=tmp_path / "workspace",
        top_k=4,
    )

    assert result["summary"]["dataset_name"] == "locomo_refined"
    assert result["summary"]["evidence_recall_macro"] == 1.0


def test_run_filesystem_proxy_eval_retrieves_expected_file_stem(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "memories").mkdir(parents=True)
    (kb / "MEMORY.md").write_text("- [image](wiki/memories/image-main.md)", encoding="utf-8")
    (kb / "wiki" / "memories" / "image-main.md").write_text(
        "koshary egypt national dish",
        encoding="utf-8",
    )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "c1",
                    "question": "which country has koshary as national dish?",
                    "silver_evidence_ids": ["image-main"],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=2,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["summary"]["metric_label_source"] == "answer_derived_proxy"
    assert result["cases"][0]["hit_evidence_ids"] == ["image-main"]


def test_run_filesystem_proxy_eval_rejects_stale_retrieval_fields(tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "turn.md").write_text("Evidence: hit\nneedle", encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "polluted",
                    "question": "needle",
                    "silver_evidence_ids": ["hit"],
                    "final_retrieved_ids": ["hit"],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stale retrieval"):
        run_filesystem_proxy_eval(
            dataset_name="meta_crag",
            cases_path=cases,
            output_dir=tmp_path / "out",
            top_k=1,
        )


def test_run_filesystem_proxy_eval_reads_yaml_evidence_id(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "memories").mkdir(parents=True)
    (kb / "wiki" / "memories" / "artifact-1.md").write_text(
        "\n".join(
            [
                "---",
                'artifact_id: "artifact-1"',
                'evidence_id: "silver-1"',
                "---",
                "",
                "# Artifact",
                "blue lighthouse dish marker",
            ]
        ),
        encoding="utf-8",
    )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "c-yaml",
                    "question": "Which artifact mentions the blue lighthouse dish marker?",
                    "silver_evidence_ids": ["silver-1"],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["hit_evidence_ids"] == ["silver-1"]


def test_run_filesystem_proxy_eval_indexes_meta_crag_retrieval_json(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "memories").mkdir(parents=True)
    (kb / ".kb-research" / "retrieval").mkdir(parents=True)
    _mark_python_workspace(kb, "meta_crag")
    memory_path = kb / "wiki" / "memories" / "artifact-2.md"
    memory_path.write_text(
        "\n".join(
            [
                "---",
                'artifact_id: "artifact-2"',
                'evidence_id: "silver-2"',
                "---",
                "",
                "# Artifact",
                "generic page text",
            ]
        ),
        encoding="utf-8",
    )
    (kb / ".kb-research" / "retrieval" / "artifact-2.json").write_text(
        json.dumps(
            {
                "artifact_id": "artifact-2",
                "evidence_id": "silver-2",
                "title": "Rare orchid propagation",
                "searchable_text": "rare orchid should be propagated in april",
                "memory_path": str(memory_path),
            }
        ),
        encoding="utf-8",
    )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "c-json",
                    "question": "Should the rare orchid be propagated in april?",
                    "silver_evidence_ids": ["silver-2"],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["hit_evidence_ids"] == ["silver-2"]


def test_run_filesystem_proxy_eval_ignores_unprovenanced_retrieval_json(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / ".kb-research" / "retrieval").mkdir(parents=True)
    (kb / ".kb-research" / "retrieval" / "borrowed.json").write_text(
        json.dumps(
            {
                "evidence_id": "silver-2",
                "title": "Borrowed ranking input",
                "searchable_text": "rare orchid should be propagated in april",
            }
        ),
        encoding="utf-8",
    )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "unprovenanced-json",
                    "question": "Should the rare orchid be propagated in april?",
                    "silver_evidence_ids": ["silver-2"],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == []


def test_run_filesystem_proxy_eval_summary_ignores_unlabeled_cases(tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "hit.md").write_text("Evidence: hit\nlabeled marker", encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "unlabeled",
                    "question": "missing marker",
                    "silver_evidence_ids": [],
                    "knowledge_base_root": str(kb),
                },
                {
                    "case_id": "labeled",
                    "question": "labeled marker",
                    "silver_evidence_ids": ["hit"],
                    "knowledge_base_root": str(kb),
                },
            ]
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["summary"]["total_cases"] == 2
    assert result["summary"]["evidence_labeled_cases"] == 1
    assert result["summary"]["recall_at_k"] == 1.0


def test_run_filesystem_proxy_eval_recomputes_meta_crag_candidates(tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "turn.md").write_text("Evidence: gold::abc123\nquestion text", encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "baseline-hit",
                    "question": "question text",
                    "silver_evidence_ids": ["gold::abc123"],
                    "baseline_retrieved_ids": ["baseline::stale"],
                    "baseline_retrieved_file_paths": ["/tmp/retrieved.txt"],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
        allow_stale_retrieval_fields=True,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["retrieved_evidence_ids"] == ["gold::abc123"]


def test_run_filesystem_proxy_eval_ignores_meta_crag_path_ids(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "turns").mkdir(parents=True)
    path = kb / "wiki" / "turns" / "t1_assistant.md"
    path.write_text("Evidence: assistant\nrare answer token", encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "meta-path-id",
                    "question": "rare answer token",
                    "silver_evidence_ids": ["assistant"],
                    "final_retrieved_ids": ["baseline::abc123"],
                    "final_retrieved_file_paths": [path.as_posix()],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
        allow_stale_retrieval_fields=True,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["retrieved_evidence_ids"] == ["assistant"]


def test_run_filesystem_proxy_eval_ignores_meta_crag_final_paths(tmp_path, monkeypatch) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "turns").mkdir(parents=True)
    path = kb / "wiki" / "turns" / "hit.md"
    path.write_text("Evidence: hit\nrare answer token", encoding="utf-8")
    stale_path = kb / "wiki" / "turns" / "stale.md"
    stale_path.write_text("Evidence: stale\nunrelated", encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "meta-final-paths",
                    "question": "rare answer token",
                    "silver_evidence_ids": ["hit"],
                    "final_retrieved_ids": ["stale"],
                    "final_retrieved_file_paths": [stale_path.as_posix()],
                    "knowledge_base_root": str(kb),
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        example_eval,
        "retrieve_qmd_consensus_files",
        lambda **_: SimpleNamespace(
            files=[
                RetrievedMemoryFile(
                    filename=path.name,
                    file_path=path.as_posix(),
                    mtime_ms=1,
                    content=path.read_text(encoding="utf-8"),
                )
            ]
        ),
    )

    result = run_filesystem_proxy_eval(
        dataset_name="meta_crag",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=12,
        allow_stale_retrieval_fields=True,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == ["hit"]


def test_run_filesystem_proxy_eval_accepts_cases_envelope(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "observations").mkdir(parents=True)
    (kb / "wiki" / "observations" / "D1_4_obs_4.md").write_text(
        "artificial intelligence education image",
        encoding="utf-8",
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "summary": {},
                "cases": [
                    {
                        "case_id": "gallery::q1",
                        "question": "Which image is about artificial intelligence education?",
                        "gold_clue_ids": ["D1:4"],
                        "knowledge_base_root": str(kb),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["summary"]["recall_at_k"] == 1.0


def test_run_filesystem_proxy_eval_recomputes_mem_gallery_clues(tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "D1_4.md").write_text("Evidence: D1:4\nartificial intelligence education image", encoding="utf-8")
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "Which image is about artificial intelligence education?",
                        "gold_clue_ids": ["D1:4"],
                        "retrieved_clue_ids": ["D9:9"],
                        "ranked_clues": [{"clue_id": "D9:9", "source_path": "/tmp/D9.md"}],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
        allow_stale_retrieval_fields=True,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["retrieved_evidence_ids"] == ["D1:4"]


def test_run_filesystem_proxy_eval_uses_mem_gallery_question_image_caption(tmp_path, monkeypatch) -> None:
    kb = tmp_path / "kb"
    (kb / ".kb-research" / "retrieval").mkdir(parents=True)
    _mark_python_workspace(kb, "mem_gallery")
    artifact = kb / ".kb-research" / "retrieval" / "artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "evidence_id": "D6:1",
                "artifact_id": "artifact-d6-1",
                "title": "artifact",
                "searchable_text": "cartoon robot artist easel palette",
            }
        ),
        encoding="utf-8",
    )
    (kb / ".kb-research" / "retrieval" / "wrong.json").write_text(
        json.dumps(
            {
                "evidence_id": "D9:9",
                "artifact_id": "artifact-d9-9",
                "title": "artifact",
                "searchable_text": "option matches provided picture",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        example_eval,
        "retrieve_qmd_consensus_files",
        lambda **_: SimpleNamespace(
            files=[
                RetrievedMemoryFile(
                    filename="wrong.json",
                    file_path=(kb / ".kb-research" / "retrieval" / "wrong.json").as_posix(),
                    mtime_ms=1,
                    content="Evidence: D9:9\noption matches provided picture",
                )
            ]
        ),
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "Which option matches the provided picture?",
                        "question_image_caption": "cartoon robot artist easel palette",
                        "gold_clue_ids": ["D6:1"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=2,
    )

    assert result["summary"]["recall_at_k"] == 1.0


def test_run_filesystem_proxy_eval_loads_mem_gallery_caption_from_dialog_root(tmp_path, monkeypatch) -> None:
    dialog_root = tmp_path / "dialog"
    dialog_root.mkdir()
    (dialog_root / "Topic.json").write_text(
        json.dumps(
            {
                "human-annotated QAs": [
                    {
                        "question": "Which option matches the provided picture?",
                        "image_caption": "cartoon robot artist easel palette",
                        "session_id": ["D6"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    kb = tmp_path / "kb"
    (kb / ".kb-research" / "retrieval").mkdir(parents=True)
    _mark_python_workspace(kb, "mem_gallery")
    (kb / ".kb-research" / "retrieval" / "artifact.json").write_text(
        json.dumps(
            {
                "evidence_id": "D6:1",
                "artifact_id": "artifact-d6-1",
                "title": "artifact",
                "searchable_text": "cartoon robot artist easel palette",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        example_eval,
        "retrieve_qmd_consensus_files",
        lambda **_: SimpleNamespace(files=[]),
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "Topic::q1",
                        "question": "Which option matches the provided picture?",
                        "gold_clue_ids": ["D6:1"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
        mem_gallery_dialog_root=dialog_root,
    )

    assert result["summary"]["recall_at_k"] == 1.0


def test_run_filesystem_proxy_eval_ignores_mem_gallery_retrieved_paths(tmp_path, monkeypatch) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "observations").mkdir(parents=True)
    (kb / "wiki" / "observations" / "D1_4_obs_4.md").write_text("Evidence: D1:4\nplain image", encoding="utf-8")
    (kb / "wiki" / "observations" / "D9_9_obs_9.md").write_text(
        "Evidence: D9:9\nmatching query terms",
        encoding="utf-8",
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "matching query terms",
                        "gold_clue_ids": ["D9:9"],
                        "retrieved_clue_ids": ["D9:9"],
                        "retrieved_file_paths": [
                            (kb / "wiki" / "observations" / "D1_4_obs_4.md").as_posix()
                        ],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    hit_path = kb / "wiki" / "observations" / "D9_9_obs_9.md"
    monkeypatch.setattr(
        example_eval,
        "retrieve_qmd_consensus_files",
        lambda **_: SimpleNamespace(
            files=[
                RetrievedMemoryFile(
                    filename=hit_path.name,
                    file_path=hit_path.as_posix(),
                    mtime_ms=1,
                    content=hit_path.read_text(encoding="utf-8"),
                )
            ]
        ),
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
        allow_stale_retrieval_fields=True,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["retrieved_evidence_ids"] == ["D9:9"]


def test_run_filesystem_proxy_eval_reads_bulleted_evidence_marker(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "observations").mkdir(parents=True)
    path = kb / "wiki" / "observations" / "D1_4_obs_4.md"
    path.write_text("- Session: D1\n- Evidence: D1:4\nplain image", encoding="utf-8")
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "plain image",
                        "gold_clue_ids": ["D1:4"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == ["D1:4"]


def test_run_filesystem_proxy_eval_maps_source_relative_turn_links(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "sources").mkdir(parents=True)
    (kb / "wiki" / "turns").mkdir(parents=True)
    source = kb / "wiki" / "sources" / "session_6.md"
    turn = kb / "wiki" / "turns" / "D6_1.md"
    turn2 = kb / "wiki" / "turns" / "D6_2.md"
    source.write_text(
        "- [turn D6:1](../turns/D6_1.md) matching query\n"
        "- [turn D6:2](../turns/D6_2.md) matching query",
        encoding="utf-8",
    )
    turn.write_text("Evidence: D6:1\nmatching query", encoding="utf-8")
    turn2.write_text("Evidence: D6:2\nmatching query", encoding="utf-8")
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "matching query",
                        "gold_clue_ids": ["D6:1", "D6:2"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == ["D6:1"]


def test_run_filesystem_proxy_eval_maps_mem_gallery_clue_summary_source(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "memories").mkdir(parents=True)
    path = kb / "wiki" / "memories" / "clue-summary-d2-24-9c29c32f.md"
    path.write_text(
        'sources: ["clue:D2:D2:24"]\n\n# Clue summary D2:24\n- source ref: clue:D2:D2:24',
        encoding="utf-8",
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "summary",
                        "gold_clue_ids": ["D2:24"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == ["D2:24"]


def test_run_filesystem_proxy_eval_prefers_mem_gallery_support_map(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "memories").mkdir(parents=True)
    (kb / ".mem-gallery").mkdir(parents=True)
    path = kb / "wiki" / "memories" / "clue-summary-d6-1.md"
    path.write_text('summary\n- source ref: clue:D6:D6:1', encoding="utf-8")
    (kb / ".mem-gallery" / "artifact_support_map.json").write_text(
        json.dumps(
            [
                {
                    "memory_path": path.as_posix(),
                    "linked_clue_ids": ["D6:IMG_001"],
                }
            ]
        ),
        encoding="utf-8",
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "summary",
                        "gold_clue_ids": ["D6:IMG_001"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == ["D6:IMG_001"]


def test_run_filesystem_proxy_eval_reads_mem_gallery_retrieval_json_support(tmp_path) -> None:
    kb = tmp_path / "kb"
    (kb / "wiki" / "memories").mkdir(parents=True)
    (kb / ".mem-gallery").mkdir(parents=True)
    (kb / ".kb-research" / "retrieval").mkdir(parents=True)
    _mark_python_workspace(kb, "mem_gallery")
    memory = kb / "wiki" / "memories" / "turn-image-d6-3.md"
    memory.write_text("image placeholder", encoding="utf-8")
    (kb / ".mem-gallery" / "artifact_support_map.json").write_text(
        json.dumps(
            [
                {
                    "memory_path": memory.as_posix(),
                    "linked_clue_ids": ["D6:IMG_001"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (kb / ".kb-research" / "retrieval" / "turn-image-d6-3.json").write_text(
        json.dumps(
            {
                "artifact_id": "turn-image-d6-3",
                "memory_path": memory.as_posix(),
                "title": "Education robot image",
                "searchable_text": "bright classroom robot tutoring students",
            }
        ),
        encoding="utf-8",
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "Which image shows a robot tutoring students?",
                        "gold_clue_ids": ["D6:IMG_001"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == ["D6:IMG_001"]


def test_mem_gallery_source_session_labels_do_not_affect_retrieval(
    tmp_path,
    monkeypatch,
) -> None:
    kb = tmp_path / "kb"
    retrieval = kb / ".kb-research" / "retrieval"
    retrieval.mkdir(parents=True)
    _mark_python_workspace(kb, "mem_gallery")
    (retrieval / "labeled-session.json").write_text(
        json.dumps(
            {
                "evidence_id": "D6:1",
                "title": "unrelated artifact",
                "searchable_text": "plain unrelated note",
            }
        ),
        encoding="utf-8",
    )
    (retrieval / "matching.json").write_text(
        json.dumps(
            {
                "evidence_id": "D9:9",
                "title": "telescope artifact",
                "searchable_text": "blue telescope on balcony",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        example_eval,
        "retrieve_qmd_consensus_files",
        lambda **_: SimpleNamespace(files=[]),
    )
    cases = tmp_path / "mem_gallery_eval.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "gallery::q",
                        "question": "Which clue mentions a blue telescope?",
                        "gold_clue_ids": ["D9:9"],
                        "source_session_ids": ["D6"],
                        "knowledge_base_root": str(kb),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["cases"][0]["retrieved_evidence_ids"] == ["D9:9"]


def test_run_filesystem_proxy_eval_reuses_workspace_read_for_same_root(tmp_path, monkeypatch) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "D1_4.md").write_text("artificial intelligence education image", encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": "gallery::q1",
                    "question": "artificial intelligence education",
                    "gold_clue_ids": ["D1:4"],
                    "knowledge_base_root": str(kb),
                },
                {
                    "case_id": "gallery::q2",
                    "question": "education image",
                    "gold_clue_ids": ["D1:4"],
                    "knowledge_base_root": str(kb),
                },
            ]
        ),
        encoding="utf-8",
    )
    calls = []
    original = getattr(example_eval, "_read_workspace_files")

    def counted(root, *, include_retrieval_json=False):
        calls.append(root)
        return original(root, include_retrieval_json=include_retrieval_json)

    monkeypatch.setattr(example_eval, "_read_workspace_files", counted)

    result = run_filesystem_proxy_eval(
        dataset_name="mem_gallery",
        cases_path=cases,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert len(calls) == 1
    assert result["summary"]["recall_at_k"] == 1.0


def test_run_evermembench_python_eval_reads_dialogue_references(tmp_path) -> None:
    topic = tmp_path / "01"
    topic.mkdir()
    (topic / "dialogue.json").write_text(
        json.dumps(
            [
                {
                    "topic_id": "01",
                    "date": "2025-01-01",
                    "dialogues": {
                        "Empty Group": None,
                        "Group 1": [
                            {
                                "speaker": "Lin",
                                "time": "2025-01-01 09:00:00",
                                "dialogue": "The peak CPU usage was 65 percent.",
                                "message_index": 4,
                            }
                        ]
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (topic / "qa_01.json").write_text(
        json.dumps(
            [
                {
                    "topic_id": "01",
                    "id": "q1",
                    "Q": "What was the peak CPU usage?",
                    "A": "65 percent",
                    "R": [{"date": "2025-01-01", "group": "Group 1", "message_index": "4"}],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_evermembench_python_eval(
        data_root=tmp_path,
        output_dir=tmp_path / "out",
        top_k=1,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["hit_evidence_ids"] == ["D1:4"]


def test_run_evermembench_python_eval_supports_topic_and_question_shards(tmp_path) -> None:
    for topic_id in ("01", "02"):
        topic = tmp_path / topic_id
        topic.mkdir()
        (topic / "dialogue.json").write_text(
            json.dumps(
                [
                    {
                        "topic_id": topic_id,
                        "date": "2025-01-01",
                        "dialogues": {
                            "Group 1": [
                                {
                                    "speaker": "Lin",
                                    "time": "2025-01-01 09:00:00",
                                    "dialogue": f"Topic {topic_id} alpha marker.",
                                    "message_index": 1,
                                },
                                {
                                    "speaker": "Lin",
                                    "time": "2025-01-01 09:01:00",
                                    "dialogue": f"Topic {topic_id} beta marker.",
                                    "message_index": 2,
                                },
                            ]
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )
        (topic / f"qa_{topic_id}.json").write_text(
            json.dumps(
                [
                    {
                        "topic_id": topic_id,
                        "id": "q1",
                        "Q": f"What alpha marker belongs to topic {topic_id}?",
                        "A": "alpha",
                        "R": [{"date": "2025-01-01", "group": "Group 1", "message_index": "1"}],
                    },
                    {
                        "topic_id": topic_id,
                        "id": "q2",
                        "Q": f"What beta marker belongs to topic {topic_id}?",
                        "A": "beta",
                        "R": [{"date": "2025-01-01", "group": "Group 1", "message_index": "2"}],
                    },
                ]
            ),
            encoding="utf-8",
        )

    result = run_evermembench_python_eval(
        data_root=tmp_path,
        output_dir=tmp_path / "out",
        top_k=2,
        topic_names=["02"],
        question_offset=1,
        question_limit=1,
    )

    assert [case["case_id"] for case in result["cases"]] == ["02::q2"]
    assert result["cases"][0]["hit_evidence_ids"] == ["D1:2"]


def test_run_evermembench_python_eval_expands_dialogue_neighbors(tmp_path) -> None:
    topic = tmp_path / "01"
    topic.mkdir()
    (topic / "dialogue.json").write_text(
        json.dumps(
            [
                {
                    "topic_id": "01",
                    "date": "2025-01-01",
                    "dialogues": {
                        "Group 1": [
                            {
                                "speaker": "Lin",
                                "time": "2025-01-01 09:00:00",
                                "dialogue": "CPU setup marker was discussed first.",
                                "message_index": 1,
                            },
                            {
                                "speaker": "Lin",
                                "time": "2025-01-01 09:01:00",
                                "dialogue": "The value was 65 percent.",
                                "message_index": 2,
                            },
                            {
                                "speaker": "Lin",
                                "time": "2025-01-01 09:02:00",
                                "dialogue": "Unrelated distractor.",
                                "message_index": 3,
                            },
                        ]
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (topic / "qa_01.json").write_text(
        json.dumps(
            [
                {
                    "topic_id": "01",
                    "id": "q-neighbor",
                    "Q": "What followed the CPU setup marker?",
                    "A": "65 percent",
                    "R": [{"date": "2025-01-01", "group": "Group 1", "message_index": "2"}],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_evermembench_python_eval(
        data_root=tmp_path,
        output_dir=tmp_path / "out",
        top_k=2,
    )

    assert result["summary"]["recall_at_k"] == 1.0
    assert result["cases"][0]["hit_evidence_ids"] == ["D1:2"]


def test_evermem_retrieval_adds_same_group_neighbors() -> None:
    rows = [
        {
            "evidence_id": "D1:1",
            "session_id": "D1",
            "date": "2025-01-01",
            "group": "Group 1",
        },
        {
            "evidence_id": "D1:2",
            "session_id": "D1",
            "date": "2025-01-01",
            "group": "Group 1",
        },
        {
            "evidence_id": "D1:3",
            "session_id": "D1",
            "date": "2025-01-01",
            "group": "Group 1",
        },
    ]
    files = []
    contents = ["CPU setup marker.", "The value was 65 percent.", "Unrelated distractor."]
    for index, content in enumerate(contents, start=1):
        files.append(
            RetrievedMemoryFile(
                filename=f"{index}.md",
                file_path=f"/{index}.md",
                mtime_ms=index,
                content=content,
            )
        )
    evidence_by_path = {
        file.file_path: row["evidence_id"] for file, row in zip(files, rows)
    }

    retrieve_evermem_evidence = getattr(example_eval, "_retrieve_evermem_evidence")
    retrieved, _ = retrieve_evermem_evidence(
        "What followed the CPU setup marker?",
        files=files,
        evidence_by_path=evidence_by_path,
        rows=rows,
        top_k=2,
    )

    assert retrieved == ["D1:1", "D1:2"]
