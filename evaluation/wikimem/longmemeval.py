"""LongMemEval retrieval-only helpers for the wikimem Python migration."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from common.llm.base import LLM

from evaluation.wikimem.retained_eval import (
    ConversationRecord,
    LoCoMoQuestion,
    ObservationNote,
    PreparedSample,
    SessionEvents,
    WikiMode,
    run_retained_qmd_eval,
)
from evaluation.wikimem.wiki_builder import WikiBuilderMode


@dataclass(frozen=True)
class LongMemEvalSummary:
    dataset_name: str
    granularity: str
    total_cases: int
    evaluated_cases: int
    skipped_abstention_cases: int
    skipped_no_target_cases: int
    averaged_metrics: dict[str, dict[str, float]]


@dataclass(frozen=True)
class LongMemEvalRetrievalResults:
    query: str
    granularity: str
    ranked_items: list[dict[str, Any]]
    ranked_item_count: int
    metrics: dict[str, dict[str, float]]


@dataclass(frozen=True)
class LongMemEvalCaseScore:
    question_id: str
    question_type: str
    question: str
    question_date: str | None
    answer: Any
    abstention_case: bool
    evaluated: bool
    expected_session_ids: list[str]
    expected_turn_ids: list[str]
    retrieval_results: LongMemEvalRetrievalResults
    knowledge_base_root: str
    kb_fallback_used: bool
    kb_warnings: list[str] = field(default_factory=list)


def load_longmemeval_samples(
    path: str | Path,
    sample_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    samples = []
    for raw in rows:
        question_id = str(raw.get("question_id", ""))
        if sample_filter is not None and question_id not in sample_filter:
            continue
        samples.append(_prepare_longmemeval_sample(raw))
    return samples


def run_python_longmemeval_retrieval_eval(
    *,
    dataset_path: str | Path,
    output_dir: str | Path,
    workspace_root: str | Path,
    top_k: int = 24,
    granularity: str = "turn",
    sample_limit: int | None = None,
    wiki_mode: WikiMode = "text",
    llm: LLM | None = None,
    wiki_builder_mode: WikiBuilderMode = "llm",
    query_llm: LLM | None = None,
) -> dict[str, Any]:
    samples = load_longmemeval_samples(dataset_path)
    if sample_limit is not None:
        samples = samples[:sample_limit]
    locomo_samples = adapt_longmemeval_to_locomo_samples(samples)
    output_dir = Path(output_dir)
    retained_output = run_retained_qmd_eval(
        dataset_name="longmemeval",
        samples=locomo_samples,
        workspace_root=workspace_root,
        top_k=top_k,
        question_limit=1,
        harness_root=output_dir / "harness",
        wiki_mode=wiki_mode,
        llm=llm,
        wiki_builder_mode=wiki_builder_mode,
        query_llm=query_llm,
    )
    result = convert_retained_output_to_longmemeval(
        samples=samples,
        retained_cases=retained_output.cases,
        granularity=granularity,
        workspace_root=workspace_root,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "longmemeval_retrieval_eval.json").write_text(
        json.dumps(_jsonable(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return _jsonable(result)


def adapt_longmemeval_to_locomo_samples(
    samples: list[dict[str, Any]],
) -> list[PreparedSample]:
    prepared = []
    for sample in samples:
        session_number_by_id = {
            session_id: index + 1
            for index, session_id in enumerate(sample.get("haystack_session_ids", []))
        }
        records: list[ConversationRecord] = []
        session_summaries: dict[int, str] = {}
        session_events: dict[int, SessionEvents] = {}
        observations: dict[int, list[ObservationNote]] = {}
        support_by_path: dict[str, dict[str, list[str]]] = {}
        turn_ids_by_speaker: dict[str, list[str]] = {}
        session_ids_by_speaker: dict[str, list[str]] = {}
        for session_index, session_id in enumerate(sample.get("haystack_session_ids", [])):
            session_number = session_number_by_id[session_id]
            session_token = f"D{session_number}"
            date = _get_index(sample.get("haystack_dates", []), session_index)
            user_ordinal = 0
            user_texts = []
            for turn_index, turn in enumerate(
                _get_index(sample.get("haystack_sessions", []), session_index) or []
            ):
                role = str(turn.get("role", ""))
                content = str(turn.get("content", "")).strip()
                if role.lower() == "user":
                    user_ordinal += 1
                    evidence_id = f"D{session_number}:{user_ordinal}"
                    turn_id = f"{session_id}_{turn_index + 1}"
                    user_texts.append(content)
                    observations.setdefault(session_number, []).append(
                        ObservationNote(speaker="User", evidence_id=evidence_id, text=content)
                    )
                    turn_ids_by_speaker.setdefault("User", []).append(turn_id)
                    session_ids_by_speaker.setdefault("User", []).append(session_id)
                    # Keep the conversion support map aligned with the Rust
                    # LongMemEval adapter.  The retained evaluator extracts
                    # evidence IDs from selected turn/event files, but those
                    # files are not registered as support pages by the Rust
                    # adapter's fallback path.  Only source/topic/entity and
                    # fallback-observation pages participate in ranked-page
                    # metrics; otherwise Python would count an extra page
                    # type and shift every later rank.
                else:
                    evidence_id = f"A{session_number}_{turn_index + 1}"
                records.append(
                    ConversationRecord(
                        dia_id=evidence_id,
                        session_id=session_token,
                        speaker=_normalize_role(role),
                        text=content,
                    )
                )
            session_summaries[session_number] = " ".join(user_texts)
            session_events[session_number] = SessionEvents(
                date=str(date) if date is not None else None,
                items_by_speaker={
                    "User": [
                        f"Evidence {note.evidence_id}: {note.text}"
                        for note in observations.get(session_number, [])
                    ]
                },
            )
            session_turn_ids = [
                f"{session_id}_{index + 1}"
                for index, turn in enumerate(
                    _get_index(sample.get("haystack_sessions", []), session_index) or []
                )
                if str(turn.get("role", "")).lower() == "user"
            ]
            session_support = {"turn_ids": session_turn_ids, "session_ids": [session_id]}
            support_by_path[f"wiki/sources/session_{session_number}.md"] = session_support
            support_by_path[
                f"wiki/topics/session_{session_number}_observations.md"
            ] = session_support
            support_by_path[f"wiki/topics/session_{session_number}_events.md"] = session_support
            for ordinal, note in enumerate(observations.get(session_number, []), start=1):
                turn_support = {
                    "turn_ids": session_turn_ids[ordinal - 1 : ordinal],
                    "session_ids": [session_id],
                }
                support_by_path[
                    f"wiki/observations/{_evidence_slug(note.evidence_id)}_obs_{ordinal}.md"
                ] = turn_support
                # Event pages intentionally have no direct support mapping in
                # the Rust fallback adapter.  Their evidence IDs are still
                # extracted by the retained evaluator, but are appended as
                # evidence support after ranked file pages during conversion.

        for speaker, turn_ids in turn_ids_by_speaker.items():
            support_by_path[f"wiki/entities/{_entity_slug(speaker)}.md"] = {
                "turn_ids": _unique(turn_ids),
                "session_ids": _unique(session_ids_by_speaker.get(speaker, [])),
            }
        sample["_locomo_support_by_path"] = support_by_path

        evidence = [
            sample.get("_locomo_evidence_by_turn_id", {}).get(turn_id)
            or _locomo_evidence_id_for_turn_id(turn_id, session_number_by_id)
            for turn_id in sample.get("answer_turn_ids", [])
        ]
        evidence = [item for item in evidence if item]
        prepared.append(
            PreparedSample(
                sample_id=str(sample.get("question_id", "")),
                raw_sample=sample,
                records=records,
                questions=[
                    LoCoMoQuestion(
                        question=str(sample.get("question", "")),
                        answer=sample.get("answer"),
                        evidence=evidence,
                        category=None,
                    )
                ],
                session_datetimes={
                    session_number_by_id[session_id]: str(date)
                    for session_id, date in zip(
                        sample.get("haystack_session_ids", []),
                        sample.get("haystack_dates", []),
                    )
                    if session_id in session_number_by_id
                },
                session_summaries=session_summaries,
                event_summaries=session_events,
                observations=observations,
            )
        )
    return prepared


def convert_retained_output_to_longmemeval(
    *,
    samples: list[dict[str, Any]],
    retained_cases: list[Any],
    granularity: str,
    workspace_root: str | Path,
) -> dict[str, Any]:
    sample_by_id = {str(sample.get("question_id", "")): sample for sample in samples}
    cases = []
    for score in retained_cases:
        sample = sample_by_id[score.sample_id]
        expected_turn_ids = list(sample.get("answer_turn_ids", []))
        expected_session_ids = list(sample.get("answer_session_ids", []))
        ranked_support_pages = _ranked_support_pages(score, sample)
        abstention_case = _is_longmemeval_abstention(sample)
        evaluated = _is_longmemeval_evaluated(
            granularity=granularity,
            abstention_case=abstention_case,
            expected_turn_ids=expected_turn_ids,
            expected_session_ids=expected_session_ids,
        )
        metrics = (
            _longmemeval_metrics(
                granularity=granularity,
                expected_turn_ids=expected_turn_ids,
                expected_session_ids=expected_session_ids,
                ranked_support_pages=ranked_support_pages,
            )
            if evaluated
            else {"turn": {}, "session": {}}
        )
        ranked_items = [
            {
                "corpus_id": _relative_retrieved_path(path, score.knowledge_base_root),
                "text": "",
                "timestamp": None,
            }
            for path in score.retrieved_file_paths
        ]
        cases.append(
            LongMemEvalCaseScore(
                question_id=score.sample_id,
                question_type=str(sample.get("question_type", "")),
                question=score.question,
                question_date=sample.get("question_date"),
                answer=sample.get("answer"),
                abstention_case=abstention_case,
                evaluated=evaluated,
                expected_session_ids=expected_session_ids,
                expected_turn_ids=expected_turn_ids,
                retrieval_results=LongMemEvalRetrievalResults(
                    query=score.question,
                    granularity=granularity,
                    ranked_items=ranked_items,
                    ranked_item_count=len(ranked_items),
                    metrics=metrics,
                ),
                knowledge_base_root=str(Path(workspace_root) / score.sample_id),
                kb_fallback_used=False,
                kb_warnings=["locomo_qmdconsensus python migration"],
            )
        )

    evaluated = [case for case in cases if case.evaluated]
    summary = LongMemEvalSummary(
        dataset_name="longmemeval",
        granularity=granularity,
        total_cases=len(cases),
        evaluated_cases=len(evaluated),
        skipped_abstention_cases=sum(1 for case in cases if case.abstention_case),
        skipped_no_target_cases=sum(
            1 for case in cases if not case.evaluated and not case.abstention_case
        ),
        averaged_metrics={
            "turn": _average_metric_bucket(
                [case.retrieval_results.metrics["turn"] for case in evaluated]
            ),
            "session": _average_metric_bucket(
                [case.retrieval_results.metrics["session"] for case in evaluated]
            ),
        },
    )
    return {"summary": summary, "cases": cases}


def _is_longmemeval_abstention(sample: dict[str, Any]) -> bool:
    return "_abs" in str(sample.get("question_id", ""))


def _is_longmemeval_evaluated(
    *,
    granularity: str,
    abstention_case: bool,
    expected_turn_ids: list[str],
    expected_session_ids: list[str],
) -> bool:
    if abstention_case:
        return False
    if granularity == "session":
        return bool(expected_session_ids)
    return bool(expected_turn_ids)


def _prepare_longmemeval_sample(raw: dict[str, Any]) -> dict[str, Any]:
    sample = dict(raw)
    session_documents = []
    turn_documents = []
    answer_turn_ids = []
    locomo_evidence_by_turn_id = {}
    for session_index, session_id in enumerate(sample.get("haystack_session_ids", [])):
        turns = _get_index(sample.get("haystack_sessions", []), session_index) or []
        date = _get_index(sample.get("haystack_dates", []), session_index)
        user_ordinal = 0
        session_texts = []
        for raw_turn_index, turn in enumerate(turns, start=1):
            if str(turn.get("role", "")).lower() != "user":
                continue
            user_ordinal += 1
            content = str(turn.get("content", "")).strip()
            turn_id = f"{session_id}_{raw_turn_index}"
            locomo_evidence_by_turn_id[turn_id] = f"D{session_index + 1}:{user_ordinal}"
            session_texts.append(content)
            turn_documents.append(
                {
                    "corpus_id": turn_id,
                    "session_id": session_id,
                    "turn_index": raw_turn_index,
                    "text": content,
                    "timestamp": date,
                }
            )
            if turn.get("has_answer") is True:
                answer_turn_ids.append(turn_id)
        session_documents.append(
            {
                "corpus_id": session_id,
                "session_id": session_id,
                "turn_index": None,
                "text": " ".join(session_texts),
                "timestamp": date,
            }
        )
    sample["session_documents"] = session_documents
    sample["turn_documents"] = turn_documents
    sample["answer_turn_ids"] = answer_turn_ids
    sample["_locomo_evidence_by_turn_id"] = locomo_evidence_by_turn_id
    sample["has_user_answer_turn"] = bool(answer_turn_ids)
    return sample


def _locomo_evidence_id_for_turn_id(
    turn_id: str,
    session_number_by_id: dict[str, int],
) -> str | None:
    session_id, _, ordinal = turn_id.rpartition("_")
    if session_id not in session_number_by_id or not ordinal.isdigit():
        return None
    return f"D{session_number_by_id[session_id]}:{int(ordinal)}"


def _longmemeval_metrics(
    *,
    granularity: str,
    expected_turn_ids: list[str],
    expected_session_ids: list[str],
    ranked_support_pages: list[dict[str, list[str]]],
) -> dict[str, dict[str, float]]:
    if granularity == "session":
        return {
            "turn": {},
            "session": _page_support_metrics(
                expected_session_ids,
                ranked_support_pages,
                "session_ids",
            ),
        }
    return {
        "turn": _page_support_metrics(expected_turn_ids, ranked_support_pages, "turn_ids"),
        "session": _page_support_metrics(
            expected_session_ids,
            ranked_support_pages,
            "session_ids",
        ),
    }


def _ranked_support_pages(score: Any, sample: dict[str, Any]) -> list[dict[str, list[str]]]:
    support_by_path = sample.get("_locomo_support_by_path", {})
    pages = []
    for path in score.retrieved_file_paths:
        relative = _relative_retrieved_path(path, score.knowledge_base_root)
        support = support_by_path.get(relative)
        if support:
            pages.append(support)

    turn_id_by_evidence = {
        evidence_id: turn_id
        for turn_id, evidence_id in sample.get("_locomo_evidence_by_turn_id", {}).items()
    }
    session_ids = sample.get("haystack_session_ids", [])
    for evidence_id in score.retrieved_evidence:
        turn_id = turn_id_by_evidence.get(evidence_id)
        session_number = evidence_id.removeprefix("D").split(":", maxsplit=1)[0]
        session_id = (
            session_ids[int(session_number) - 1]
            if session_number.isdigit() and 0 < int(session_number) <= len(session_ids)
            else None
        )
        if turn_id or session_id:
            pages.append(
                {
                    "turn_ids": [turn_id] if turn_id else [],
                    "session_ids": [session_id] if session_id else [],
                }
            )
    return pages


def _relative_retrieved_path(path: str, knowledge_root: str) -> str:
    normalized = path.replace("\\", "/")
    root = knowledge_root.replace("\\", "/").rstrip("/")
    prefix = f"{root}/"
    return normalized[len(prefix) :] if normalized.startswith(prefix) else normalized.lstrip("/")


def _page_support_metrics(
    expected: list[str],
    pages: list[dict[str, list[str]]],
    support_key: str,
) -> dict[str, float]:
    expected_unique = _unique(expected)
    metrics = {}
    for k in (1, 3, 5, 10, 30, 50):
        first_rank: dict[str, int] = {}
        for rank, page in enumerate(pages[:k], start=1):
            for item in page[support_key]:
                first_rank.setdefault(item, rank)
        hits = [item for item in expected_unique if item in first_rank]
        best_rank = min((first_rank[item] for item in hits), default=None)
        metrics[f"recall_any@{k}"] = 1.0 if hits else 0.0
        metrics[f"recall_all@{k}"] = (
            1.0 if expected_unique and len(hits) == len(expected_unique) else 0.0
        )
        # The Rust comparison table calls this ``recall_avg``: average the
        # fraction of expected evidence items recovered by each case.  It is
        # distinct from the binary any/all indicators above.
        metrics[f"recall_avg@{k}"] = (
            len(hits) / len(expected_unique) if expected_unique else 0.0
        )
        metrics[f"ndcg_any@{k}"] = (
            _round4(_discounted_gain_for_rank(best_rank)) if best_rank is not None else 0.0
        )
    return metrics


def _discounted_gain_for_rank(rank: int) -> float:
    if rank <= 1:
        return 1.0
    import math

    return 1.0 / math.log2(rank + 1)


def _average_metric_bucket(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {}
    keys = sorted({key for row in rows for key in row})
    return {
        key: _round4(sum(row.get(key, 0.0) for row in rows) / len(rows))
        for key in keys
    }


def _normalize_role(role: str) -> str:
    return "User" if role.lower() == "user" else "Assistant"


def _get_index(values: list[Any], index: int) -> Any:
    return values[index] if 0 <= index < len(values) else None


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _evidence_slug(evidence_id: str) -> str:
    return evidence_id.replace(":", "_")


def _entity_slug(value: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in value)
    return slug.strip("_") or "entity"


def _round4(value: float) -> float:
    return round(value + 0.0, 4)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value
