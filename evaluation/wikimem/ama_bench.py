"""AMA-Bench retrieval-only helpers for the wikimem Python migration."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evaluation.wikimem.qmd_consensus import RetrievedMemoryFile
from evaluation.wikimem.retained_eval import WikiMode, normalize_wiki_mode
from evaluation.wikimem.retrieval_profile import retrieve_qmd_consensus_files


@dataclass(frozen=True)
class AmaTurn:
    turn_idx: int
    action: str = ""
    observation: str = ""


@dataclass(frozen=True)
class AmaQuestion:
    question: str
    answer: str
    qa_type: str = ""
    options: list[str] | None = None


@dataclass(frozen=True)
class AmaEpisode:
    episode_id: str
    task: str
    task_type: str
    domain: str
    trajectory: list[AmaTurn]
    qa_pairs: list[AmaQuestion]


@dataclass(frozen=True)
class ProxyRetrievalMetrics:
    proxy_gold_turn_ids: list[int]
    retrieved_turn_ids: list[int]
    hit_turn_ids: list[int]
    proxy_recall_at_k: float
    proxy_precision_at_k: float
    proxy_hit_at_k: float
    answer_support_coverage: float


@dataclass(frozen=True)
class AmaMethodQuestionResult:
    method_name: str
    episode_id: str
    domain: str
    task_type: str
    qa_type: str
    question_index: int
    question: str
    golden_answer: str
    proxy_metrics: ProxyRetrievalMetrics
    retrieved_context: str
    retrieved_turn_ids: list[int]
    retrieved_file_paths: list[str]
    retrieval_notes: list[str]


@dataclass(frozen=True)
class AmaMethodSummary:
    method_name: str
    questions: int
    proxy_recall_at_k: float
    proxy_precision_at_k: float
    proxy_hit_at_k: float
    answer_support_coverage: float
    judged_questions: int = 0
    judge_accuracy: float | None = None


def load_ama_episodes_from_jsonl(
    path: str | Path,
    limit: int | None = None,
) -> list[AmaEpisode]:
    episodes = []
    buffer = ""
    start_line = 1
    lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    for line_number, line in enumerate(lines, 1):
        if not buffer and not line.strip():
            continue
        if not buffer:
            start_line = line_number
        buffer += line
        stripped = buffer.strip()
        try:
            raw = json.loads(_escape_json_string_newlines(stripped))
        except json.JSONDecodeError as error:
            if line_number != len(lines):
                continue
            raise ValueError(f"failed to parse AMA episode line {start_line}: {error}") from error
        episodes.append(_parse_ama_episode(raw))
        buffer = ""
        if limit is not None and len(episodes) >= limit:
            break
    if buffer.strip():
        raise ValueError(f"failed to parse AMA episode line {start_line}: incomplete JSON record")
    return episodes


def load_ama_dataset_split(
    dataset_root: str | Path,
    subset: str = "open_end",
    sample_limit: int | None = None,
    question_limit: int | None = None,
) -> list[AmaEpisode]:
    file_name = "open_end_qa_set.jsonl" if subset == "open_end" else "mcq_set.jsonl"
    episodes = load_ama_episodes_from_jsonl(
        Path(dataset_root) / "test" / file_name,
        limit=sample_limit,
    )
    if question_limit is not None:
        episodes = [
            AmaEpisode(
                episode_id=episode.episode_id,
                task=episode.task,
                task_type=episode.task_type,
                domain=episode.domain,
                trajectory=episode.trajectory,
                qa_pairs=episode.qa_pairs[:question_limit],
            )
            for episode in episodes
        ]
    return episodes


def score_ama_retrieval_proxy(
    episode: AmaEpisode,
    question: AmaQuestion,
    retrieved_turn_ids: list[int],
) -> ProxyRetrievalMetrics:
    gold_turn_ids = derive_proxy_gold_turn_ids(episode, question)
    gold_set = set(gold_turn_ids)
    unique_retrieved = _unique_preserve_order(retrieved_turn_ids)
    hit_turn_ids = [turn_id for turn_id in unique_retrieved if turn_id in gold_set]
    recall = _ratio(len(hit_turn_ids), len(gold_turn_ids))
    precision = _ratio(len(hit_turn_ids), len(unique_retrieved))
    return ProxyRetrievalMetrics(
        proxy_gold_turn_ids=gold_turn_ids,
        retrieved_turn_ids=unique_retrieved,
        hit_turn_ids=hit_turn_ids,
        proxy_recall_at_k=_round4(recall),
        proxy_precision_at_k=_round4(precision),
        proxy_hit_at_k=1.0 if hit_turn_ids else 0.0,
        answer_support_coverage=_round4(recall),
    )


def derive_proxy_gold_turn_ids(episode: AmaEpisode, question: AmaQuestion) -> list[int]:
    answer_tokens = _normalize_token_set(question.answer)
    question_tokens = _normalize_token_set(question.question)
    scored = []
    for turn in episode.trajectory:
        turn_tokens = _normalize_token_set(f"{turn.action} {turn.observation}")
        answer_overlap = len(answer_tokens & turn_tokens)
        question_overlap = len(question_tokens & turn_tokens)
        scored.append(((answer_overlap * 2) + question_overlap, answer_overlap, turn.turn_idx))
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    positives = [
        turn_idx
        for score, answer_overlap, turn_idx in scored
        if score >= 2 and answer_overlap >= 1
    ]
    if not positives:
        positives = [
            turn_idx
            for _, answer_overlap, turn_idx in scored
            if answer_overlap >= 1
        ][:1]
    return sorted(set(positives))


def run_python_wikimem_qmd_retrieval(
    episode: AmaEpisode,
    question: AmaQuestion,
    *,
    top_k: int,
    method_name: str = "wikimem_qmd",
    wiki_mode: WikiMode = "text",
) -> AmaMethodQuestionResult:
    wiki_mode = normalize_wiki_mode(wiki_mode)
    if method_name == "wikimem_qmd_ama":
        lexical_turn_ids = select_ama_lexical_turn_ids(
            episode.trajectory,
            question.question,
            top_k=top_k,
        )
        files = _build_ama_lexical_files(episode, lexical_turn_ids)
        metrics = score_ama_retrieval_proxy(episode, question, lexical_turn_ids)
        return AmaMethodQuestionResult(
            method_name=method_name,
            episode_id=episode.episode_id,
            domain=episode.domain,
            task_type=episode.task_type,
            qa_type=question.qa_type,
            question_index=0,
            question=question.question,
            golden_answer=question.answer,
            proxy_metrics=metrics,
            retrieved_context=_format_retrieved_context(files),
            retrieved_turn_ids=_unique_preserve_order(lexical_turn_ids),
            retrieved_file_paths=[file.file_path for file in files],
            retrieval_notes=[
                "retrieval_plugins=qmd_consensus,ama_anchor_consensus",
                "ama_merge_mode=hybrid_fusion",
                "proxy_turn_source=ama_lexical_primary",
                "qmd_context_supplement=skipped_in_retrieval_only_mode",
            ],
        )

    files = build_ama_memory_files(episode, wiki_mode=wiki_mode)
    root_files = [file for file in files if "/wiki/sources/" in file.file_path]
    result = retrieve_qmd_consensus_files(
        question=question.question,
        files=files,
        root_files=root_files,
        entity_names=_candidate_entity_names(episode, question),
        top_k=top_k,
    )
    retrieved_turn_ids = []
    for file in result.files:
        turn_id = _extract_turn_id_from_path(file.file_path)
        if turn_id is not None:
            retrieved_turn_ids.append(turn_id)
    metrics = score_ama_retrieval_proxy(episode, question, retrieved_turn_ids)
    return AmaMethodQuestionResult(
        method_name=method_name,
        episode_id=episode.episode_id,
        domain=episode.domain,
        task_type=episode.task_type,
        qa_type=question.qa_type,
        question_index=0,
        question=question.question,
        golden_answer=question.answer,
        proxy_metrics=metrics,
        retrieved_context=_format_retrieved_context(result.files),
        retrieved_turn_ids=_unique_preserve_order(retrieved_turn_ids),
        retrieved_file_paths=[file.file_path for file in result.files],
        retrieval_notes=[
            "retrieval_plugins=qmd_consensus",
            f"coverage_late_bridge={len(result.coverage.late_bridge_file_paths)}",
        ],
    )


def select_ama_lexical_turn_ids(
    turns: list[AmaTurn],
    question: str,
    top_k: int,
) -> list[int]:
    top_k = max(top_k, 1)
    query_tokens = _normalize_token_set(question)
    scored = [
        (
            len(query_tokens & _normalize_token_set(_format_ama_turn(turn))),
            turn.turn_idx,
        )
        for turn in turns
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [turn_idx for score, turn_idx in scored[:top_k] if score > 0]
    if selected:
        return selected
    return [turn_idx for _, turn_idx in scored[:top_k]]


def build_ama_memory_files(
    episode: AmaEpisode,
    *,
    wiki_mode: WikiMode = "text",
) -> list[RetrievedMemoryFile]:
    """Build AMA's text-only wiki; multimodal mode is accepted for API parity.

    AMA-Bench trajectories currently contain action/observation text only, so
    selecting ``multimodal`` does not synthesize image artifacts or alter the
    searchable corpus.
    """
    normalize_wiki_mode(wiki_mode)
    source_content = [
        "## Summary",
        f"AMA episode {episode.episode_id}: {episode.task}",
        "",
        "## Turn Index",
    ]
    files = []
    for turn in episode.trajectory:
        source_content.append(f"- [turn {turn.turn_idx}](../turns/T{turn.turn_idx}.md)")
        files.append(
            RetrievedMemoryFile(
                filename=f"T{turn.turn_idx}.md",
                file_path=f"/ama/{episode.episode_id}/wiki/turns/T{turn.turn_idx}.md",
                mtime_ms=turn.turn_idx,
                content=(
                    f"- Session: D1\n- Evidence: T{turn.turn_idx}\n"
                    f"Action: {turn.action}\nObservation: {turn.observation}"
                ),
            )
        )
        files.append(
            RetrievedMemoryFile(
                filename=f"D1_T{turn.turn_idx}_obs.md",
                file_path=(
                    f"/ama/{episode.episode_id}/wiki/observations/"
                    f"D1_T{turn.turn_idx}_obs.md"
                ),
                mtime_ms=turn.turn_idx,
                content=(
                    f"- Session: D1\n- Evidence: T{turn.turn_idx}\n"
                    f"{turn.action}\n{turn.observation}"
                ),
            )
        )

    files.insert(
        0,
        RetrievedMemoryFile(
            filename="session_1.md",
            file_path=f"/ama/{episode.episode_id}/wiki/sources/session_1.md",
            mtime_ms=0,
            content="\n".join(source_content),
        ),
    )
    return files


def aggregate_ama_method_summaries(
    results: list[AmaMethodQuestionResult],
) -> list[AmaMethodSummary]:
    grouped: dict[str, list[AmaMethodQuestionResult]] = {}
    for result in results:
        grouped.setdefault(result.method_name, []).append(result)
    return [
        AmaMethodSummary(
            method_name=method_name,
            questions=len(rows),
            proxy_recall_at_k=_round4(
                _mean([row.proxy_metrics.proxy_recall_at_k for row in rows])
            ),
            proxy_precision_at_k=_round4(
                _mean([row.proxy_metrics.proxy_precision_at_k for row in rows])
            ),
            proxy_hit_at_k=_round4(_mean([row.proxy_metrics.proxy_hit_at_k for row in rows])),
            answer_support_coverage=_round4(
                _mean([row.proxy_metrics.answer_support_coverage for row in rows])
            ),
        )
        for method_name, rows in sorted(grouped.items())
    ]


def build_ama_precision_comparison(
    *,
    python_summary: AmaMethodSummary | dict[str, Any],
    rust_baseline: AmaMethodSummary | dict[str, Any],
    tolerance: float = 0.0,
) -> dict[str, Any]:
    python_values = _summary_mapping(python_summary)
    baseline_values = _summary_mapping(rust_baseline)
    metrics = [
        "proxy_recall_at_k",
        "proxy_precision_at_k",
        "proxy_hit_at_k",
        "answer_support_coverage",
    ]
    deltas = {}
    statuses = {}
    for metric in metrics:
        if metric not in python_values or metric not in baseline_values:
            continue
        delta = _round4(float(python_values[metric]) - float(baseline_values[metric]))
        deltas[metric] = delta
        statuses[metric] = "pass" if delta + tolerance >= 0.0 else "regression"

    return {
        "method_name": python_values.get("method_name", baseline_values.get("method_name", "")),
        "questions": python_values.get("questions"),
        "baseline_questions": baseline_values.get("questions"),
        "status": "pass"
        if all(status == "pass" for status in statuses.values())
        else "regression",
        "tolerance": tolerance,
        "python_summary": python_values,
        "rust_baseline": baseline_values,
        "metric_deltas": deltas,
        "metric_status": statuses,
    }


def run_python_ama_retrieval_eval(
    *,
    dataset_root: str | Path,
    output_dir: str | Path,
    subset: str = "open_end",
    top_k: int = 24,
    sample_limit: int | None = None,
    question_limit: int | None = None,
    methods: list[str] | None = None,
    rust_baseline: AmaMethodSummary | dict[str, Any] | None = None,
    tolerance: float = 0.0,
    wiki_mode: WikiMode = "text",
) -> dict[str, Any]:
    wiki_mode = normalize_wiki_mode(wiki_mode)
    episodes = load_ama_dataset_split(
        dataset_root,
        subset,
        sample_limit=sample_limit,
        question_limit=question_limit,
    )
    method_names = methods or ["wikimem_qmd"]
    results = []
    for method_name in method_names:
        for episode in episodes:
            for question_index, question in enumerate(episode.qa_pairs):
                result = run_python_wikimem_qmd_retrieval(
                    episode,
                    question,
                    top_k=top_k,
                    method_name=method_name,
                    wiki_mode=wiki_mode,
                )
                results.append(
                    AmaMethodQuestionResult(
                        method_name=result.method_name,
                        episode_id=result.episode_id,
                        domain=result.domain,
                        task_type=result.task_type,
                        qa_type=result.qa_type,
                        question_index=question_index,
                        question=result.question,
                        golden_answer=result.golden_answer,
                        proxy_metrics=result.proxy_metrics,
                        retrieved_context=result.retrieved_context,
                        retrieved_turn_ids=result.retrieved_turn_ids,
                        retrieved_file_paths=result.retrieved_file_paths,
                        retrieval_notes=result.retrieval_notes,
                    )
                )

    method_summaries = aggregate_ama_method_summaries(results)
    stem = "openend" if subset == "open_end" else subset
    config = {
        "dataset_root": str(dataset_root),
        "subset": subset,
        "mode": "retrieval_only",
        "output_dir": str(output_dir),
        "sample_limit": sample_limit,
        "question_limit": question_limit,
        "methods": method_names,
        "top_k": top_k,
        "wiki_mode": wiki_mode,
        "effective_wiki_mode": "text",
    }
    report = {
        "config": config,
        "metric_label_source": "answer_derived_proxy",
        "total_episodes": len(episodes),
        "total_questions": len(results),
        "method_summaries": [_jsonable(summary) for summary in method_summaries],
        "question_results": [_jsonable(result) for result in results],
    }
    summary = {
        "config": config,
        "metric_label_source": "answer_derived_proxy",
        "total_episodes": len(episodes),
        "total_questions": len(results),
        "method_summaries": [_jsonable(summary) for summary in method_summaries],
        "domain_summaries": _aggregate_ama_breakdown_summaries(results, "domain"),
        "task_type_summaries": _aggregate_ama_breakdown_summaries(results, "task_type"),
        "qa_type_summaries": _aggregate_ama_breakdown_summaries(results, "qa_type"),
    }
    comparison = (
        build_ama_precision_comparison(
            python_summary=method_summaries[0],
            rust_baseline=rust_baseline,
            tolerance=tolerance,
        )
        if rust_baseline is not None and method_summaries
        else None
    )

    output = Path(output_dir)
    _write_json(output / f"report_{stem}.json", report)
    _write_json(output / f"summary_{stem}.json", summary)
    if comparison is not None:
        _write_json(output / f"comparison_{stem}.json", comparison)
    return {
        "report": report,
        "summary": summary,
        "comparison": comparison,
    }


def _parse_ama_episode(raw: dict[str, Any]) -> AmaEpisode:
    return AmaEpisode(
        episode_id=str(raw.get("episode_id", "")),
        task=str(raw.get("task", "")),
        task_type=str(raw.get("task_type", "")),
        domain=str(raw.get("domain", "")),
        trajectory=[
            AmaTurn(
                turn_idx=int(turn.get("turn_idx", 0)),
                action=str(turn.get("action", "")),
                observation=str(turn.get("observation", "")),
            )
            for turn in raw.get("trajectory", [])
            if isinstance(turn, dict)
        ],
        qa_pairs=[
            AmaQuestion(
                question=str(question.get("question", "")),
                answer=str(question.get("answer", "")),
                qa_type=str(question.get("type", "")),
                options=[str(item) for item in question.get("options", [])],
            )
            for question in raw.get("qa_pairs", [])
            if isinstance(question, dict)
        ],
    )


def _candidate_entity_names(episode: AmaEpisode, question: AmaQuestion) -> list[str]:
    text = f"{episode.task} {question.question}"
    return re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", text)


def _extract_turn_id_from_path(path: str) -> int | None:
    normalized = path.replace("\\", "/")
    match = re.search(r"/turns/t?(\d+)\.md$", normalized, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"_t(\d+)_obs\.md$", normalized, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _format_retrieved_context(files: list[RetrievedMemoryFile]) -> str:
    return "\n\n".join(f"## {file.file_path}\n{file.content}" for file in files)


def _build_ama_lexical_files(
    episode: AmaEpisode,
    turn_ids: list[int],
) -> list[RetrievedMemoryFile]:
    turns_by_id = {turn.turn_idx: turn for turn in episode.trajectory}
    files = []
    for turn_id in turn_ids:
        turn = turns_by_id.get(turn_id)
        if turn is None:
            continue
        files.append(
            RetrievedMemoryFile(
                filename=f"T{turn.turn_idx}.md",
                file_path=f"ama_turns/T{turn.turn_idx}.md",
                mtime_ms=turn.turn_idx,
                content=_format_ama_turn(turn),
            )
        )
    return files


def _format_ama_turn(turn: AmaTurn) -> str:
    return (
        f"Turn {turn.turn_idx}:\n"
        f"Action: {turn.action}\n"
        f"Observation: {turn.observation}"
    )


def _normalize_token_set(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.split(r"[^0-9A-Za-z]+", text)
        if token.strip()
    }


def _unique_preserve_order(values: list[int]) -> list[int]:
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _summary_mapping(summary: AmaMethodSummary | dict[str, Any]) -> dict[str, Any]:
    if isinstance(summary, AmaMethodSummary):
        return {
            "method_name": summary.method_name,
            "questions": summary.questions,
            "proxy_recall_at_k": summary.proxy_recall_at_k,
            "proxy_precision_at_k": summary.proxy_precision_at_k,
            "proxy_hit_at_k": summary.proxy_hit_at_k,
            "answer_support_coverage": summary.answer_support_coverage,
            "judged_questions": summary.judged_questions,
            "judge_accuracy": summary.judge_accuracy,
        }
    return dict(summary)


def _aggregate_ama_breakdown_summaries(
    results: list[AmaMethodQuestionResult],
    field_name: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[AmaMethodQuestionResult]] = {}
    for result in results:
        group_name = str(getattr(result, field_name))
        grouped.setdefault((result.method_name, group_name), []).append(result)
    summaries = []
    for (method_name, group_name), rows in sorted(grouped.items()):
        summary = aggregate_ama_method_summaries(rows)[0]
        values = _summary_mapping(summary)
        values["group_name"] = group_name
        values["method_name"] = method_name
        summaries.append(values)
    return summaries


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _escape_json_string_newlines(text: str) -> str:
    escaped = []
    in_string = False
    escape_next = False
    for char in text:
        if escape_next:
            escaped.append(char)
            escape_next = False
            continue
        if char == "\\":
            escaped.append(char)
            escape_next = True
            continue
        if char == '"':
            escaped.append(char)
            in_string = not in_string
            continue
        if char == "\n" and in_string:
            escaped.append("\\n")
            continue
        escaped.append(char)
    return "".join(escaped)


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _round4(value: float) -> float:
    return round(value + 0.0, 4)
