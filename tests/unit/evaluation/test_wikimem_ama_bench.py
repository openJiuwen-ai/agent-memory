"""AMA-Bench retrieval-only harness compatibility tests."""

from __future__ import annotations

import json

import pytest

from evaluation.wikimem.ama_bench import (
    AmaEpisode,
    AmaQuestion,
    AmaTurn,
    aggregate_ama_method_summaries,
    build_ama_precision_comparison,
    load_ama_dataset_split,
    load_ama_episodes_from_jsonl,
    run_python_ama_retrieval_eval,
    run_python_wikimem_qmd_retrieval,
    score_ama_retrieval_proxy,
    select_ama_lexical_turn_ids,
)

pytestmark = pytest.mark.unit


def test_load_ama_episodes_and_score_proxy_metrics(tmp_path) -> None:
    dataset = tmp_path / "open_end_qa_set.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "episode_id": 7,
                "domain": "household",
                "task_type": "open",
                "trajectory": [
                    {
                        "turn_idx": 1,
                        "action": "open drawer",
                        "observation": "The drawer contains brass tools.",
                    },
                    {
                        "turn_idx": 2,
                        "action": "inspect shelf",
                        "observation": "The shelf contains paper.",
                    },
                ],
                "qa_pairs": [
                    {
                        "question": "Where were the brass tools?",
                        "answer": "drawer",
                        "type": "open_end",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    episodes = load_ama_episodes_from_jsonl(dataset)
    metrics = score_ama_retrieval_proxy(
        episodes[0],
        episodes[0].qa_pairs[0],
        retrieved_turn_ids=[1, 2, 1],
    )

    assert episodes[0].episode_id == "7"
    assert metrics.proxy_gold_turn_ids == [1]
    assert metrics.retrieved_turn_ids == [1, 2]
    assert metrics.hit_turn_ids == [1]
    assert metrics.proxy_recall_at_k == 1.0
    assert metrics.proxy_precision_at_k == 0.5
    assert metrics.proxy_hit_at_k == 1.0


def test_load_ama_episodes_tolerates_multiline_json_records(tmp_path) -> None:
    dataset = tmp_path / "open_end_qa_set.jsonl"
    dataset.write_text(
        '{"episode_id":"ep-1","trajectory":[{"turn_idx":1,'
        '"observation":"first line\n'
        'second line"}],"qa_pairs":[{"question":"q","answer":"line"}]}\n',
        encoding="utf-8",
    )

    episodes = load_ama_episodes_from_jsonl(dataset)

    assert episodes[0].trajectory[0].observation == "first line\nsecond line"


def test_load_ama_dataset_split_stops_after_sample_limit(tmp_path) -> None:
    dataset_root = tmp_path / "dataset"
    dataset = dataset_root / "test" / "open_end_qa_set.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps({"episode_id": "ep-1", "trajectory": [], "qa_pairs": []})
        + "\n"
        + '{"episode_id": "broken"\n',
        encoding="utf-8",
    )

    episodes = load_ama_dataset_split(dataset_root, "open_end", sample_limit=1)

    assert [episode.episode_id for episode in episodes] == ["ep-1"]


def test_python_wikimem_qmd_retrieval_produces_rust_summary_shape() -> None:
    episode = AmaEpisode(
        episode_id="ep-1",
        task="find tools",
        task_type="open",
        domain="household",
        trajectory=[
            AmaTurn(
                turn_idx=1,
                action="open drawer",
                observation="Alice found brass tools in the drawer.",
            ),
            AmaTurn(
                turn_idx=2,
                action="check shelf",
                observation="Alice found paper labels on the shelf.",
            ),
        ],
        qa_pairs=[
            AmaQuestion(
                question="What did Alice find in the drawer?",
                answer="brass tools",
                qa_type="open_end",
            )
        ],
    )

    result = run_python_wikimem_qmd_retrieval(
        episode,
        episode.qa_pairs[0],
        top_k=3,
    )
    summaries = aggregate_ama_method_summaries([result])

    assert result.method_name == "wikimem_qmd"
    assert result.proxy_metrics.proxy_gold_turn_ids == [1]
    assert 1 in result.retrieved_turn_ids
    assert result.proxy_metrics.proxy_recall_at_k == 1.0
    assert summaries[0].method_name == "wikimem_qmd"
    assert summaries[0].proxy_recall_at_k == 1.0
    assert summaries[0].proxy_precision_at_k >= 0.5


def test_select_ama_lexical_turn_ids_matches_rust_turn_token_ordering() -> None:
    turns = [
        AmaTurn(
            turn_idx=0,
            action="inspect file",
            observation="saw the same repeated placeholder",
        ),
        AmaTurn(
            turn_idx=7,
            action="inspect file",
            observation="saw the same repeated placeholder",
        ),
    ]

    turn_ids = select_ama_lexical_turn_ids(
        turns,
        "At step 7, what exactly did the agent inspect?",
        top_k=1,
    )

    assert turn_ids == [7]


def test_wikimem_qmd_ama_retrieval_uses_ama_lexical_primary() -> None:
    episode = AmaEpisode(
        episode_id="ep-ama",
        task="inspect release notes",
        task_type="open",
        domain="software",
        trajectory=[
            AmaTurn(
                turn_idx=0,
                action="open browser",
                observation="search page loaded",
            ),
            AmaTurn(
                turn_idx=1,
                action="search changelog",
                observation="release note mentions the delta sync fix",
            ),
            AmaTurn(
                turn_idx=2,
                action="open ticket",
                observation="ticket summary is unrelated",
            ),
        ],
        qa_pairs=[
            AmaQuestion(
                question="Which turn mentions the delta sync fix?",
                answer="turn 1",
                qa_type="open_end",
            )
        ],
    )

    result = run_python_wikimem_qmd_retrieval(
        episode,
        episode.qa_pairs[0],
        top_k=2,
        method_name="wikimem_qmd_ama",
    )

    assert result.method_name == "wikimem_qmd_ama"
    assert result.retrieved_turn_ids == [1, 0]
    assert result.retrieved_file_paths == ["ama_turns/T1.md", "ama_turns/T0.md"]
    assert "proxy_turn_source=ama_lexical_primary" in result.retrieval_notes


def test_ama_golden_answer_does_not_affect_retrieval_ranking() -> None:
    episode = AmaEpisode(
        episode_id="ep-no-leak",
        task="inspect notes",
        task_type="open",
        domain="software",
        trajectory=[
            AmaTurn(turn_idx=0, action="open note", observation="alpha release"),
            AmaTurn(turn_idx=1, action="open note", observation="beta release"),
        ],
        qa_pairs=[],
    )
    first = run_python_wikimem_qmd_retrieval(
        episode,
        AmaQuestion(question="Which note mentions beta?", answer="turn 1", qa_type="open"),
        top_k=2,
        method_name="wikimem_qmd_ama",
    )
    second = run_python_wikimem_qmd_retrieval(
        episode,
        AmaQuestion(
            question="Which note mentions beta?",
            answer="deliberately different gold answer",
            qa_type="open",
        ),
        top_k=2,
        method_name="wikimem_qmd_ama",
    )

    assert first.retrieved_turn_ids == second.retrieved_turn_ids
    assert first.retrieved_file_paths == second.retrieved_file_paths


def test_ama_precision_comparison_marks_regressions_against_rust_baseline() -> None:
    comparison = build_ama_precision_comparison(
        python_summary={
            "method_name": "wikimem_qmd",
            "questions": 2,
            "proxy_recall_at_k": 0.7,
            "proxy_precision_at_k": 0.8,
            "proxy_hit_at_k": 1.0,
        },
        rust_baseline={
            "method_name": "wikimem_qmd",
            "questions": 2,
            "proxy_recall_at_k": 0.75,
            "proxy_precision_at_k": 0.79,
            "proxy_hit_at_k": 1.0,
        },
        tolerance=0.001,
    )

    assert comparison["status"] == "regression"
    assert comparison["method_name"] == "wikimem_qmd"
    assert comparison["metric_deltas"]["proxy_recall_at_k"] == -0.05
    assert comparison["metric_status"]["proxy_recall_at_k"] == "regression"
    assert comparison["metric_status"]["proxy_precision_at_k"] == "pass"


def test_run_python_ama_retrieval_eval_writes_report_summary_and_comparison(tmp_path) -> None:
    dataset_root = tmp_path / "dataset"
    dataset_file = dataset_root / "test" / "open_end_qa_set.jsonl"
    dataset_file.parent.mkdir(parents=True)
    dataset_file.write_text(
        json.dumps(
            {
                "episode_id": "ep-1",
                "domain": "household",
                "task_type": "open",
                "trajectory": [
                    {
                        "turn_idx": 1,
                        "action": "open drawer",
                        "observation": "Alice found brass tools in the drawer.",
                    }
                ],
                "qa_pairs": [
                    {
                        "question": "What did Alice find in the drawer?",
                        "answer": "brass tools",
                        "type": "open_end",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    result = run_python_ama_retrieval_eval(
        dataset_root=dataset_root,
        output_dir=output_dir,
        subset="open_end",
        top_k=3,
        rust_baseline={
            "method_name": "wikimem_qmd",
            "questions": 1,
            "proxy_recall_at_k": 1.0,
            "proxy_precision_at_k": 1.0,
            "proxy_hit_at_k": 1.0,
            "answer_support_coverage": 1.0,
        },
    )

    report = json.loads((output_dir / "report_openend.json").read_text(encoding="utf-8"))
    summary = json.loads((output_dir / "summary_openend.json").read_text(encoding="utf-8"))
    comparison = json.loads(
        (output_dir / "comparison_openend.json").read_text(encoding="utf-8")
    )
    assert result["comparison"]["status"] == "pass"
    assert report["metric_label_source"] == "answer_derived_proxy"
    assert summary["metric_label_source"] == "answer_derived_proxy"
    assert report["total_episodes"] == 1
    assert summary["method_summaries"][0]["method_name"] == "wikimem_qmd"
    assert comparison["metric_status"]["proxy_recall_at_k"] == "pass"


def test_run_python_ama_retrieval_eval_accepts_qmd_ama_method(tmp_path) -> None:
    dataset_root = tmp_path / "dataset"
    dataset_file = dataset_root / "test" / "open_end_qa_set.jsonl"
    dataset_file.parent.mkdir(parents=True)
    dataset_file.write_text(
        json.dumps(
            {
                "episode_id": "ep-ama",
                "domain": "software",
                "task_type": "open",
                "trajectory": [
                    {
                        "turn_idx": 0,
                        "action": "open browser",
                        "observation": "search page loaded",
                    },
                    {
                        "turn_idx": 1,
                        "action": "search changelog",
                        "observation": "release note mentions the delta sync fix",
                    },
                ],
                "qa_pairs": [
                    {
                        "question": "Which turn mentions the delta sync fix?",
                        "answer": "turn 1",
                        "type": "open_end",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_python_ama_retrieval_eval(
        dataset_root=dataset_root,
        output_dir=tmp_path / "output",
        subset="open_end",
        top_k=2,
        methods=["wikimem_qmd_ama"],
    )

    assert result["summary"]["config"]["methods"] == ["wikimem_qmd_ama"]
    assert result["summary"]["method_summaries"][0]["method_name"] == "wikimem_qmd_ama"
