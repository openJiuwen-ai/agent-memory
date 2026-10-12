"""wikimem retained_eval DTOs and deterministic harness helpers."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from common.llm.base import LLM
from evaluation.wikimem.llm_semantics import SemanticSource
from evaluation.wikimem.qmd_consensus import (
    RetrievedMemoryFile,
    normalize_memory_path,
    score_line,
)
from evaluation.wikimem.retrieval_profile import retrieve_qmd_consensus_files
from evaluation.wikimem.wiki_builder import WikiBuilder, WikiBuilderMode

WikiMode = Literal["text", "multimodal"]


def normalize_wiki_mode(value: str) -> WikiMode:
    """Validate the workspace materialization mode shared by all evaluators."""

    if value not in {"text", "multimodal"}:
        raise ValueError(f"unsupported wiki_mode: {value!r}; expected 'text' or 'multimodal'")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class LoCoMoQuestion:
    question: str
    answer: Any = None
    adversarial_answer: str | None = None
    evidence: list[str] = field(default_factory=list)
    category: int | None = None


@dataclass(frozen=True)
class ConversationRecord:
    dia_id: str
    session_id: str
    speaker: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservationNote:
    speaker: str
    evidence_id: str
    text: str


@dataclass(frozen=True)
class SessionEvents:
    date: str | None = None
    items_by_speaker: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedSample:
    sample_id: str
    raw_sample: dict[str, Any]
    records: list[ConversationRecord]
    questions: list[LoCoMoQuestion]
    session_datetimes: dict[int, str]
    session_summaries: dict[int, str]
    event_summaries: dict[int, SessionEvents]
    observations: dict[int, list[ObservationNote]]


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    sample_id: str
    question_index: int
    question: str
    answer: str
    category: int | None
    expected_evidence: list[str]


@dataclass(frozen=True)
class RetrievalCoverageSummary:
    miss_stage: str
    root_hit_evidence: list[str] = field(default_factory=list)
    candidate_pool_hit_evidence: list[str] = field(default_factory=list)
    late_bridge_hit_evidence: list[str] = field(default_factory=list)
    final_hit_evidence: list[str] = field(default_factory=list)
    root_file_paths: list[str] = field(default_factory=list)
    candidate_pool_file_paths: list[str] = field(default_factory=list)
    late_bridge_file_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    sample_id: str
    question_index: int
    question: str
    category: int | None
    expected_evidence: list[str]
    retrieved_evidence: list[str]
    hit_evidence: list[str]
    retrieved_record_ids: list[str]
    retrieved_file_paths: list[str]
    retrieved_entrypoint_paths: list[str]
    knowledge_base_root: str
    retrieved_record_count: int
    retrieved_file_count: int
    retrieved_entrypoint_count: int
    evidence_precision: float
    evidence_recall: float
    full_evidence_hit: bool
    retrieval_coverage: RetrievalCoverageSummary | None = None


@dataclass(frozen=True)
class EvalSummary:
    dataset_name: str
    total_cases: int
    cases_with_evidence: int
    evidence_precision_macro: float
    evidence_precision_micro: float
    evidence_recall_macro: float
    evidence_recall_micro: float
    full_evidence_hit_rate: float


@dataclass(frozen=True)
class CategoryScoreSummary:
    category: int
    label: str
    count: int
    evidence_precision_macro: float
    evidence_recall_macro: float
    full_evidence_hit_rate: float


@dataclass(frozen=True)
class StageTimingRecord:
    stage: str
    calls: int
    total_ms: float
    avg_ms: float
    max_ms: float


@dataclass(frozen=True)
class StageProfileArtifact:
    created_at_ms: int
    total_samples: int
    total_cases: int
    stages: list[StageTimingRecord]

    def with_additional_stage(self, stage: str, duration_ms: float) -> StageProfileArtifact:
        elapsed_ms = _round4(duration_ms)
        stages = list(self.stages)
        for index, record in enumerate(stages):
            if record.stage != stage:
                continue
            calls = record.calls + 1
            total_ms = _round4(record.total_ms + elapsed_ms)
            stages[index] = StageTimingRecord(
                stage=record.stage,
                calls=calls,
                total_ms=total_ms,
                avg_ms=_round4(total_ms / max(calls, 1)),
                max_ms=_round4(max(record.max_ms, elapsed_ms)),
            )
            break
        else:
            stages.append(
                StageTimingRecord(
                    stage=stage,
                    calls=1,
                    total_ms=elapsed_ms,
                    avg_ms=elapsed_ms,
                    max_ms=elapsed_ms,
                )
            )

        return StageProfileArtifact(
            created_at_ms=self.created_at_ms,
            total_samples=self.total_samples,
            total_cases=self.total_cases,
            stages=_sort_stage_timings(stages),
        )


@dataclass
class _StageTimingTotals:
    calls: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0


class StageProfiler:
    """Aggregate retained_eval timing records using the Rust artifact shape."""

    def __init__(self) -> None:
        self._stages: dict[str, _StageTimingTotals] = {}

    def record_elapsed(self, stage: str, started_at: float) -> None:
        self.record_duration_ms(stage, (time.perf_counter() - started_at) * 1000.0)

    def record_duration(self, stage: str, duration_seconds: float) -> None:
        self.record_duration_ms(stage, duration_seconds * 1000.0)

    def record_duration_ms(self, stage: str, duration_ms: float) -> None:
        totals = self._stages.setdefault(stage, _StageTimingTotals())
        totals.calls += 1
        totals.total_ms += duration_ms
        totals.max_ms = max(totals.max_ms, duration_ms)

    def snapshot(
        self,
        total_samples: int,
        total_cases: int,
        created_at_ms: int | None = None,
    ) -> StageProfileArtifact:
        stages = [
            StageTimingRecord(
                stage=stage,
                calls=totals.calls,
                total_ms=_round4(totals.total_ms),
                avg_ms=_round4(totals.total_ms / max(totals.calls, 1)),
                max_ms=_round4(totals.max_ms),
            )
            for stage, totals in self._stages.items()
        ]
        return StageProfileArtifact(
            created_at_ms=_unix_now_ms() if created_at_ms is None else created_at_ms,
            total_samples=total_samples,
            total_cases=total_cases,
            stages=_sort_stage_timings(stages),
        )


@dataclass(frozen=True)
class EvalOutput:
    summary: EvalSummary
    cases: list[CaseScore]
    stage_profile: StageProfileArtifact


@dataclass(frozen=True)
class EvalHarnessConfig:
    dataset_name: str
    samples: str | None
    question_limit: int | None
    top_k: int
    workspace_root: str
    llm_provider: str | None
    retrieval_plugins: list[str] = field(default_factory=list)
    wiki_mode: WikiMode = "text"
    wiki_builder_mode: WikiBuilderMode = "deterministic"


@dataclass(frozen=True)
class ProgressUpdate:
    completed_cases: int
    total_cases: int
    sample_index: int
    total_samples: int
    sample_id: str
    question_index: int
    sample_question_total: int


def prepare_locomo_samples(
    payloads: list[dict[str, Any]],
    sample_filter: set[str] | None = None,
    include_multimodal_context: bool = False,
) -> list[PreparedSample]:
    prepared: list[PreparedSample] = []
    for sample in payloads:
        if not isinstance(sample, dict) or not isinstance(sample.get("sample_id"), str):
            raise ValueError("LoCoMo samples require a string sample_id")
        sample_id = sample["sample_id"]
        if sample_filter is not None and sample_id not in sample_filter:
            continue
        conversation = sample.get("conversation", {})
        qa = sample.get("qa", [])
        session_summary = sample.get("session_summary", {})
        event_summary = sample.get("event_summary", {})
        observation = sample.get("observation", {})
        if not isinstance(qa, list):
            raise ValueError(f"LoCoMo sample {sample_id} qa must be an array")
        if not isinstance(session_summary, dict):
            raise ValueError(f"LoCoMo sample {sample_id} session_summary must be an object")
        if not isinstance(event_summary, dict):
            raise ValueError(f"LoCoMo sample {sample_id} event_summary must be an object")
        if not isinstance(observation, dict):
            raise ValueError(f"LoCoMo sample {sample_id} observation must be an object")
        prepared.append(
            PreparedSample(
                sample_id=sample_id,
                raw_sample=sample,
                records=extract_conversation_records(
                    conversation,
                    include_multimodal_context=include_multimodal_context,
                ),
                questions=_parse_questions(qa),
                session_datetimes=_parse_session_datetimes(conversation),
                session_summaries=_parse_session_summaries(session_summary),
                event_summaries=_parse_event_summaries(event_summary),
                observations=_parse_observations(observation),
            )
        )
    return prepared


def extract_conversation_records(
    conversation: dict[str, Any],
    *,
    include_multimodal_context: bool = False,
) -> list[ConversationRecord]:
    if not isinstance(conversation, dict):
        raise ValueError("conversation payload must be an object")
    sessions: list[tuple[int, list[dict[str, Any]]]] = []
    for key, value in conversation.items():
        number = _session_number(key)
        if number is None:
            continue
        if not isinstance(value, list):
            raise ValueError(f"session_{number} conversation must be an array")
        sessions.append((number, value))
    sessions.sort(key=lambda item: item[0])

    records: list[ConversationRecord] = []
    for number, turns in sessions:
        for turn_index, turn in enumerate(turns, start=1):
            if not isinstance(turn, dict):
                raise ValueError(f"session_{number} contains a non-object dialogue turn")
            speaker = turn.get("speaker")
            dia_id = turn.get("dia_id")
            if dia_id is None:
                # Some LoCoMo-compatible payloads omit the Rust evidence id.
                # The dataset convention is one-based dialogue ordinals within
                # each session, so recover the same stable id before building
                # the wiki and evaluation cases.
                dia_id = f"D{number}:{turn_index}"
            text_value = turn.get("text")
            if not isinstance(speaker, str) or not isinstance(dia_id, str) or not isinstance(
                text_value, str
            ):
                raise ValueError(
                    f"session_{number} dialogue turns require string speaker, dia_id, and text"
                )
            text = text_value.strip()
            query = ""
            images: list[str] = []
            caption = turn.get("blip_caption")
            if caption is not None and not isinstance(caption, str):
                raise ValueError(f"session_{number} blip_caption must be a string or null")
            if include_multimodal_context:
                parts = [text]
                if caption and caption.strip():
                    parts.append(f"[caption: {caption.strip()}]")
                raw_query = turn.get("query")
                if raw_query is not None and not isinstance(raw_query, str):
                    raise ValueError(f"session_{number} query must be a string or null")
                query = raw_query or ""
                if query and query.strip():
                    parts.append(f"[query: {query.strip()}]")
                images = _normalize_refined_image_list(turn.get("img_url"))
                if images:
                    parts.append(f"[images: {', '.join(images)}]")
                text = " ".join(parts)
            elif caption is not None:
                # Rust's retained LoCoMo parser appends a present caption even
                # when it trims to an empty string.  Keep that text-only path
                # unchanged; refined samples use the branch above.
                text = f"{text} [caption: {caption.strip()}]"
            metadata = {}
            raw_metadata = {
                "blip_caption": caption or "",
                "query": query,
                "img_url": ",".join(images),
            }
            for key, value in raw_metadata.items():
                if value:
                    metadata[key] = value
            records.append(
                ConversationRecord(
                    dia_id=dia_id,
                    session_id=f"D{number}",
                    speaker=speaker,
                    text=text,
                    metadata=metadata,
                )
            )
    return records


def parse_sample_filter(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or raw.lower() == "all":
        return None

    result: set[str] = set()
    for token in raw.replace("/", ",").replace(";", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            result.add(f"conv-{int(token)}")
        elif token.startswith("conv-") and token[5:].isdigit():
            result.add(f"conv-{int(token[5:])}")
        else:
            result.add(token)
    return result


def build_eval_cases(
    samples: list[PreparedSample],
    question_limit: int | None = None,
) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for sample in samples:
        for index, question in enumerate(sample.questions):
            if question_limit is not None and index >= question_limit:
                break
            cases.append(
                EvalCase(
                    case_id=f"{sample.sample_id}::q{index + 1}",
                    sample_id=sample.sample_id,
                    question_index=index,
                    question=question.question,
                    answer=_stringify_answer(question),
                    category=question.category,
                    expected_evidence=list(question.evidence),
                )
            )
    return cases


def parse_retrieval_plugin_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [
        _normalize_plugin_name(token)
        for token in value.split(",")
        if _normalize_plugin_name(token)
    ]


def summarize_scores(dataset_name: str, scores: list[CaseScore]) -> EvalSummary:
    cases_with_evidence = sum(1 for score in scores if score.expected_evidence)
    total_hits = sum(len(score.hit_evidence) for score in scores)
    total_retrieved = sum(len(score.retrieved_evidence) for score in scores)
    total_expected = sum(len(score.expected_evidence) for score in scores)
    full_hit_cases = sum(1 for score in scores if score.full_evidence_hit)
    return EvalSummary(
        dataset_name=dataset_name,
        total_cases=len(scores),
        cases_with_evidence=cases_with_evidence,
        evidence_precision_macro=_round4(_macro([score.evidence_precision for score in scores])),
        evidence_precision_micro=_round4(_ratio(total_hits, total_retrieved)),
        evidence_recall_macro=_round4(_macro([score.evidence_recall for score in scores])),
        evidence_recall_micro=_round4(_ratio(total_hits, total_expected)),
        full_evidence_hit_rate=_round4(_ratio(full_hit_cases, cases_with_evidence)),
    )


def summarize_scores_by_locomo_category(scores: list[CaseScore]) -> list[CategoryScoreSummary]:
    grouped: dict[int, list[CaseScore]] = {}
    for score in scores:
        if score.category is not None:
            grouped.setdefault(score.category, []).append(score)

    summaries: list[CategoryScoreSummary] = []
    for category in sorted(grouped):
        label = _locomo_category_label(category)
        if label is None:
            continue
        bucket = grouped.get(category, [])
        cases_with_evidence = sum(1 for score in bucket if score.expected_evidence)
        full_hit_cases = sum(1 for score in bucket if score.full_evidence_hit)
        summaries.append(
            CategoryScoreSummary(
                category=category,
                label=label,
                count=len(bucket),
                evidence_precision_macro=_round4(
                    _macro([score.evidence_precision for score in bucket])
                ),
                evidence_recall_macro=_round4(
                    _macro([score.evidence_recall for score in bucket])
                ),
                full_evidence_hit_rate=_round4(_ratio(full_hit_cases, cases_with_evidence)),
            )
        )
    return summaries


def run_retained_qmd_eval(
    *,
    dataset_name: str,
    samples: list[PreparedSample],
    workspace_root: str | Path,
    top_k: int = 24,
    question_limit: int | None = None,
    retrieval_plugins: list[str] | None = None,
    harness_root: str | Path | None = None,
    wiki_mode: WikiMode = "text",
    llm: LLM | None = None,
    wiki_builder_mode: WikiBuilderMode = "llm",
    query_llm: LLM | None = None,
) -> EvalOutput:
    """Build and retrieve a retained wiki with an explicit modality mode.

    ``wiki_mode="text"`` keeps the deterministic text-only workspace used for
    Rust-compatible LoCoMo/LongMemEval runs. ``wiki_mode="multimodal"`` adds
    one ``wiki/memories/*_multimodal.md`` page for each turn carrying image,
    caption, or image-query metadata; it never downloads images or calls a
    vision model.
    """
    wiki_mode = normalize_wiki_mode(wiki_mode)
    profiler = StageProfiler()
    scores: list[CaseScore] = []
    cases = build_eval_cases(samples, question_limit)
    completed_cases = 0
    for sample in samples:
        sample_started = time.perf_counter()
        sample_root = Path(workspace_root) / sample.sample_id
        if llm is not None and wiki_builder_mode == "llm":
            semantic_sources = [
                SemanticSource(
                    source_id=record.dia_id,
                    text=record.text,
                    conversation_id=sample.sample_id,
                    session_id=record.session_id,
                    speaker=record.speaker,
                    timestamp=sample.session_datetimes.get(_record_session_number(record), ""),
                    metadata=record.metadata,
                )
                for record in sample.records
            ]
            build_result = WikiBuilder(llm=llm, mode="llm").build(
                semantic_sources, sample_root, wiki_mode=wiki_mode
            )
            files = (
                build_retained_memory_files(sample, sample_root, wiki_mode=wiki_mode)
                if (
                    not build_result.diagnostics.llm_used
                    and build_result.diagnostics.fallback_reason
                )
                else build_result.files
            )
        else:
            files = build_retained_memory_files(sample, sample_root, wiki_mode=wiki_mode)
        files = _materialize_rust_workspace_files(files)
        profiler.record_elapsed("workspace_build", sample_started)
        for question_index, question in enumerate(sample.questions):
            if question_limit is not None and question_index >= question_limit:
                break
            retrieve_started = time.perf_counter()
            result = retrieve_qmd_consensus_files(
                question=question.question,
                files=files,
                entity_names=_sample_entity_names(sample, files),
                top_k=top_k,
                knowledge_root=Path(workspace_root) / sample.sample_id,
                retrieval_plugins=retrieval_plugins,
                llm=(
                    query_llm
                    if query_llm is not None
                    else (llm if wiki_builder_mode == "llm" else None)
                ),
            )
            profiler.record_elapsed("qmd_consensus_retrieve", retrieve_started)
            expected = _normalize_expected_evidence(question.evidence)
            retrieved_ids = []
            for file in result.files:
                retrieved_ids.extend(
                    _extract_retrieved_evidence_ids(
                        file,
                        question.question,
                        result.profile,
                    )
                )
            retrieved = _ordered_unique(retrieved_ids)
            hits = [evidence_id for evidence_id in retrieved if evidence_id in set(expected)]
            scores.append(
                CaseScore(
                    case_id=f"{sample.sample_id}::q{question_index + 1}",
                    sample_id=sample.sample_id,
                    question_index=question_index,
                    question=question.question,
                    category=question.category,
                    expected_evidence=expected,
                    retrieved_evidence=retrieved,
                    hit_evidence=hits,
                    retrieved_record_ids=[],
                    retrieved_file_paths=[file.file_path for file in result.files],
                    retrieved_entrypoint_paths=[],
                    knowledge_base_root=str(Path(workspace_root) / sample.sample_id),
                    retrieved_record_count=0,
                    retrieved_file_count=len(result.files),
                    retrieved_entrypoint_count=0,
                    evidence_precision=_ratio(len(hits), len(retrieved)),
                    evidence_recall=_ratio(len(hits), len(expected)),
                    full_evidence_hit=bool(expected) and set(expected) <= set(hits),
                    retrieval_coverage=_coverage_from_profile_result(expected, result),
                )
            )
            completed_cases += 1

    output = EvalOutput(
        summary=summarize_scores(dataset_name, scores),
        cases=scores,
        stage_profile=profiler.snapshot(
            total_samples=len(samples),
            total_cases=len(cases) if cases else completed_cases,
        ),
    )
    if harness_root is not None:
        write_harness_artifacts(
            harness_root,
            output,
            EvalHarnessConfig(
                dataset_name=dataset_name,
                samples=None,
                question_limit=question_limit,
                top_k=top_k,
                workspace_root=str(workspace_root),
                llm_provider=(
                    type(llm).__name__
                    if llm is not None
                    else type(query_llm).__name__
                    if query_llm is not None
                    else None
                ),
                retrieval_plugins=retrieval_plugins or ["qmd_consensus"],
                wiki_mode=wiki_mode,
                wiki_builder_mode=wiki_builder_mode,
            ),
        )
    return output


def run_python_locomo_retrieval_eval(
    *,
    dataset_path: str | Path,
    output_dir: str | Path,
    workspace_root: str | Path,
    top_k: int = 24,
    sample_limit: int | None = None,
    question_limit: int | None = None,
    sample_filter: set[str] | None = None,
    wiki_mode: WikiMode = "text",
    llm: LLM | None = None,
    wiki_builder_mode: WikiBuilderMode = "llm",
    query_llm: LLM | None = None,
) -> dict[str, Any]:
    """Run LoCoMo retrieval with ``text`` or ``multimodal`` wiki generation."""
    payloads = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    if sample_limit is not None:
        payloads = payloads[:sample_limit]
    samples = prepare_locomo_samples(payloads, sample_filter)
    output_dir = Path(output_dir)
    output = run_retained_qmd_eval(
        dataset_name="locomo",
        samples=samples,
        workspace_root=workspace_root,
        top_k=top_k,
        question_limit=question_limit,
        harness_root=output_dir / "harness",
        wiki_mode=wiki_mode,
        llm=llm,
        wiki_builder_mode=wiki_builder_mode,
        query_llm=query_llm,
    )
    result = {
        "summary": asdict(output.summary),
        "cases": [asdict(case) for case in output.cases],
        "stage_profile": asdict(output.stage_profile),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "locomo_retrieval_eval.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def build_retained_memory_files(
    sample: PreparedSample,
    sample_root: str | Path,
    include_multiview: bool = True,
    wiki_mode: WikiMode = "multimodal",
) -> list[RetrievedMemoryFile]:
    """Materialize the retained wiki in text or multimodal mode.

    ``include_multiview`` remains a legacy switch for the minimal MEMORY.md
    projection; when it is false, the minimal path intentionally skips all
    adjunct pages. ``wiki_mode`` controls whether multimodal adjunct pages are
    present in the full retained workspace.
    """
    wiki_mode = normalize_wiki_mode(wiki_mode)
    root = Path(sample_root).as_posix()
    files: list[RetrievedMemoryFile] = []
    memory_lines = [
        f"# Memory Index for {sample.sample_id}",
        "",
        (
            "This memory index points to the most relevant cross-session profile, source "
            "transcripts, observations, and event timelines."
        ),
        "",
        "- [profile](wiki/synthesis/profile.md) - cross-session profile and recurring themes",
    ]
    for session_number in sorted(sample.session_summaries):
        session_records = [
            item for item in sample.records if item.session_id == f"D{session_number}"
        ]
        session_summary = sample.session_summaries[session_number]
        source_lines = [
            "---",
            f"description: Session {session_number} summary with links to atomic dialogue turns",
            "type: project",
            "---",
            f"# Session {session_number}",
            "",
            "## Session Date",
            sample.session_datetimes.get(session_number, "Unknown"),
            "",
            "## Summary",
            session_summary,
            "",
            "## Turn Index",
        ]
        for record in session_records:
            slug = _evidence_slug(record.dia_id)
            source_lines.append(
                f"- [turn {record.dia_id}](../turns/{slug}.md) "
                f"{record.speaker}: {_truncate_preview(record.text, 120)}"
            )
        source_lines.append("")
        files.append(
            RetrievedMemoryFile(
                filename=f"session_{session_number}.md",
                file_path=f"{root}/wiki/sources/session_{session_number}.md",
                mtime_ms=len(files) + 1,
                content="\n".join(source_lines),
                description=f"session {session_number} summary and turn index",
            )
        )
        memory_lines.append(
            f"- [session_{session_number} source](wiki/sources/session_{session_number}.md) "
            "- session summary and turn index"
        )

    for record in sample.records:
        slug = _evidence_slug(record.dia_id)
        session_number = _record_session_number(record)
        date_line = (
            f"- Session date: {sample.session_datetimes[session_number]}\n"
            if session_number in sample.session_datetimes
            else ""
        )
        date_description = (
            f" on {sample.session_datetimes[session_number]}"
            if session_number in sample.session_datetimes
            else ""
        )
        files.append(
            RetrievedMemoryFile(
                filename=f"{slug}.md",
                file_path=f"{root}/wiki/turns/{slug}.md",
                mtime_ms=len(files) + 1,
                content=(
                    f"---\ndescription: Dialogue turn {record.dia_id} in session "
                    f"{record.session_id}{date_description} where {record.speaker} says: "
                    f"{_truncate_preview(record.text, 140)}\ntype: project\n---\n"
                    f"# Turn {record.dia_id}\n\n"
                    f"- Session: {record.session_id}\n"
                    f"{date_line}"
                    f"- Speaker: {record.speaker}\n"
                    f"- Evidence: {record.dia_id}\n\n"
                    f"## Content\n{record.text}\n"
                ),
            )
        )

    if not include_multiview:
        files.insert(
            0,
            RetrievedMemoryFile(
                filename="MEMORY.md",
                file_path=f"{root}/MEMORY.md",
                mtime_ms=0,
                content="\n".join(memory_lines),
                description=f"Retained memory index for {sample.sample_id}",
            ),
        )
        return files

    for session_number, notes in sorted(sample.observations.items()):
        topic_lines = [
            "---",
            (
                f"description: Observation index for session {session_number} with links to "
                "atomic evidence notes"
            ),
            "type: project",
            "---",
            f"# Session {session_number} Observations",
            "",
        ]
        for ordinal, note in enumerate(notes, start=1):
            slug = _evidence_slug(note.evidence_id)
            topic_lines.append(
                f"- [observation {note.evidence_id}]"
                f"(../observations/{slug}_obs_{ordinal}.md) "
                f"{note.speaker}: {_truncate_preview(note.text, 120)}"
            )
            files.append(
                RetrievedMemoryFile(
                    filename=f"{slug}_obs_{ordinal}.md",
                    file_path=f"{root}/wiki/observations/{slug}_obs_{ordinal}.md",
                    mtime_ms=len(files) + 1,
                    content=(
                        f"---\ndescription: Observation {note.evidence_id} for session "
                        f"{session_number} about {note.speaker}: "
                        f"{_truncate_preview(note.text, 140)}\n"
                        f"type: project\n---\n# Observation {ordinal}\n\n"
                        f"- Session: D{session_number}\n- Speaker: {note.speaker}\n"
                        f"- Evidence: {note.evidence_id}\n\n## Note\n{note.text}\n"
                    ),
                )
            )
        topic_lines.append("")
        files.append(
            RetrievedMemoryFile(
                filename=f"session_{session_number}_observations.md",
                file_path=f"{root}/wiki/topics/session_{session_number}_observations.md",
                mtime_ms=len(files) + 1,
                content="\n".join(topic_lines),
            )
        )
        memory_lines.append(
            f"- [session_{session_number} observations]"
            f"(wiki/topics/session_{session_number}_observations.md) "
            "- evidence-grounded observation index"
        )

    for session_number, events in sorted(sample.event_summaries.items()):
        topic_lines = [
            "---",
            (
                f"description: Event index for session {session_number} with dated atomic "
                "event notes"
            ),
            "type: project",
            "---",
            f"# Session {session_number} Events",
            "",
        ]
        if events.date is not None:
            topic_lines.extend([f"Date: {events.date}", ""])
        ordinal = 0
        for speaker, entries in sorted(events.items_by_speaker.items()):
            for entry in entries:
                ordinal += 1
                topic_lines.append(
                    f"- [event {ordinal}]"
                    f"(../events/session_{session_number}_event_{ordinal}.md) "
                    f"{speaker}: {_truncate_preview(entry, 120)}"
                )
                date_line = (
                    f"- Date: {events.date}\n" if events.date is not None else ""
                )
                files.append(
                    RetrievedMemoryFile(
                        filename=f"session_{session_number}_event_{ordinal}.md",
                        file_path=(
                            f"{root}/wiki/events/session_{session_number}_event_{ordinal}.md"
                        ),
                        mtime_ms=len(files) + 1,
                        content=(
                            f"---\ndescription: Session {session_number} event {ordinal} "
                            f"about {speaker}: {_truncate_preview(entry, 140)}\n"
                            "type: project\n---\n"
                            f"# Event {ordinal}\n\n- Session: D{session_number}\n"
                            f"{date_line}- Speaker: {speaker}\n\n## Event\n{entry}\n"
                        ),
                    )
                )
        topic_lines.append("")
        files.append(
            RetrievedMemoryFile(
                filename=f"session_{session_number}_events.md",
                file_path=f"{root}/wiki/topics/session_{session_number}_events.md",
                mtime_ms=len(files) + 1,
                content="\n".join(topic_lines),
            )
        )
        memory_lines.append(
            f"- [session_{session_number} events]"
            f"(wiki/topics/session_{session_number}_events.md) - dated event index"
        )

    for speaker in _sample_entity_names(sample):
        entity_slug = _entity_slug(speaker)
        files.append(
            RetrievedMemoryFile(
                filename=f"{entity_slug}.md",
                file_path=f"{root}/wiki/entities/{entity_slug}.md",
                mtime_ms=len(files) + 1,
                content=_render_retained_entity_page(sample, speaker),
                description=f"Cross-session profile for {speaker}",
            )
        )

    evidence_lines = [
        f"- [{note.evidence_id}] {note.speaker}: {note.text}"
        for notes in sample.observations.values()
        for note in notes
    ][:8]
    files.append(
        RetrievedMemoryFile(
            filename="profile.md",
            file_path=f"{root}/wiki/synthesis/profile.md",
            mtime_ms=len(files) + 1,
            content=(
                f"---\ndescription: Cross-session profile for {sample.sample_id}\n"
                "type: project\n---\n# Profile\n\n"
                "This page summarizes recurring facts and themes across the conversation.\n\n"
                "## Evidence Highlights\n"
                + "\n".join(evidence_lines)
                + "\n"
            ),
        )
    )

    if wiki_mode == "multimodal":
        for multimodal in _iter_multimodal_turns(sample):
            artifact_slug = f"{_evidence_slug(multimodal['evidence_id'])}_multimodal"
            memory_lines.append(
                f"- [{multimodal['evidence_id']} multimodal]"
                f"(wiki/memories/{artifact_slug}.md) - image/caption/query adjunct"
            )
            files.append(
                RetrievedMemoryFile(
                    filename=f"{artifact_slug}.md",
                    file_path=f"{root}/wiki/memories/{artifact_slug}.md",
                    mtime_ms=len(files) + 1,
                    content=_render_multimodal_memory_page(multimodal),
                )
            )

    files.extend(
        [
            RetrievedMemoryFile(
                filename="index.md",
                file_path=f"{root}/index.md",
                mtime_ms=len(files) + 1,
                content=(
                    f"---\ndescription: Index of LoCoMo knowledge pages for "
                    f"{sample.sample_id}\ntype: reference\n---\n# Index\n\n"
                    "- [[wiki/synthesis/profile.md]]\n"
                    "- [[wiki/sources]] session transcript pages\n"
                    "- [[wiki/topics]] observation and event pages\n"
                    + (
                        "- [[wiki/memories]] multimodal adjunct pages\n"
                        if wiki_mode == "multimodal"
                        else ""
                    )
                ),
            ),
            RetrievedMemoryFile(
                filename="log.md",
                file_path=f"{root}/log.md",
                mtime_ms=len(files) + 2,
                content=(
                    f"---\ndescription: Build log for {sample.sample_id}\ntype: reference\n"
                    "---\n- Materialized LoCoMo sample into local memory knowledge base.\n"
                ),
            ),
            RetrievedMemoryFile(
                filename=".wiki-schema.md",
                file_path=f"{root}/.wiki-schema.md",
                mtime_ms=0,
                content=(
                    f"# Wiki Schema\n\n- topic: LoCoMo {sample.sample_id}\n"
                    "- language: en\n- structure: raw + wiki\n"
                    "- purpose: persistent memory retrieval evaluation\n"
                ),
            ),
            RetrievedMemoryFile(
                filename="sample.json",
                file_path=f"{root}/raw/sample.json",
                mtime_ms=0,
                content=json.dumps(sample.raw_sample, indent=2, ensure_ascii=False),
            ),
        ]
    )

    files.insert(
        0,
        RetrievedMemoryFile(
            filename="MEMORY.md",
            file_path=f"{root}/MEMORY.md",
            mtime_ms=0,
            content="\n".join(memory_lines) + "\n",
            description=f"Retained memory index for {sample.sample_id}",
        ),
    )
    return _assign_rust_scaffold_mtimes(files, root)


def _assign_rust_scaffold_mtimes(
    files: list[RetrievedMemoryFile],
    root: str,
) -> list[RetrievedMemoryFile]:
    """Use the same deterministic creation order as Rust's scaffold_memory."""

    normalized = {normalize_memory_path(file.file_path): file for file in files}
    ordered: list[RetrievedMemoryFile] = []
    seen: set[str] = set()

    def take(path: str) -> None:
        key = normalize_memory_path(path)
        file = normalized.get(key)
        if file is not None and key not in seen:
            ordered.append(file)
            seen.add(key)

    for path in (
        f"{root}/.wiki-schema.md",
        f"{root}/index.md",
        f"{root}/log.md",
        f"{root}/MEMORY.md",
        f"{root}/raw/sample.json",
    ):
        take(path)

    def matching(prefix: str) -> list[RetrievedMemoryFile]:
        prefix_key = normalize_memory_path(prefix)
        matches = []
        for file in files:
            normalized_path = normalize_memory_path(file.file_path)
            if normalized_path.startswith(prefix_key) and normalized_path not in seen:
                matches.append(file)
        return matches

    source_files = matching(f"{root}/wiki/sources/")
    source_files.sort(
        key=lambda file: int(re.search(r"session_(\d+)\.md$", file.file_path).group(1))
        if re.search(r"session_(\d+)\.md$", file.file_path)
        else 0
    )
    for file in source_files:
        take(file.file_path)

    for file in files:
        if "/wiki/turns/" in normalize_memory_path(file.file_path):
            take(file.file_path)

    observation_topics = matching(f"{root}/wiki/topics/")
    observation_topics = [
        file for file in observation_topics if "_observations.md" in file.file_path
    ]
    observation_topics.sort(
        key=lambda file: int(re.search(r"session_(\d+)_observations\.md$", file.file_path).group(1))
        if re.search(r"session_(\d+)_observations\.md$", file.file_path)
        else 0
    )
    for topic in observation_topics:
        session_match = re.search(r"session_(\d+)_observations\.md$", topic.file_path)
        session_number = int(session_match.group(1)) if session_match else 0
        take(topic.file_path)
        observation_files = []
        for file in matching(f"{root}/wiki/observations/"):
            match = re.search(r"_obs_(\d+)\.md$", file.file_path)
            prefix_match = re.search(r"(D\d+)_", Path(file.file_path).name)
            if (
                match is not None
                and prefix_match is not None
                and int(prefix_match.group(1)[1:]) == session_number
            ):
                observation_files.append(file)
        observation_files.sort(
            key=lambda file: int(re.search(r"_obs_(\d+)\.md$", file.file_path).group(1))
        )
        for file in observation_files:
            take(file.file_path)

    event_topics = matching(f"{root}/wiki/topics/")
    event_topics = [file for file in event_topics if "_events.md" in file.file_path]
    event_topics.sort(
        key=lambda file: int(re.search(r"session_(\d+)_events\.md$", file.file_path).group(1))
        if re.search(r"session_(\d+)_events\.md$", file.file_path)
        else 0
    )
    for topic in event_topics:
        session_match = re.search(r"session_(\d+)_events\.md$", topic.file_path)
        session_number = int(session_match.group(1)) if session_match else 0
        take(topic.file_path)
        event_files = []
        for file in matching(f"{root}/wiki/events/"):
            match = re.search(r"session_(\d+)_event_(\d+)\.md$", file.file_path)
            if match is not None and int(match.group(1)) == session_number:
                event_files.append(file)
        event_files.sort(
            key=lambda file: int(re.search(r"_event_(\d+)\.md$", file.file_path).group(1))
        )
        for file in event_files:
            take(file.file_path)

    for file in sorted(matching(f"{root}/wiki/entities/"), key=lambda item: item.filename):
        take(file.file_path)
    take(f"{root}/wiki/synthesis/profile.md")
    for file in files:
        take(file.file_path)
    # Rust's scan uses filesystem mtimes only as a recency tie-breaker.  Keep
    # a deterministic logical write clock in the same scaffold order instead
    # of importing timestamps or group boundaries from a reference run.
    write_clock = {
        normalize_memory_path(file.file_path): index
        for index, file in enumerate(ordered, start=1)
    }
    return [
        replace(
            file,
            mtime_ms=write_clock.get(normalize_memory_path(file.file_path), 0),
        )
        for file in files
    ]


def _materialize_rust_workspace_files(
    files: list[RetrievedMemoryFile],
) -> list[RetrievedMemoryFile]:
    """Write the virtual wiki in Rust scaffold order and retain filesystem mtimes.

    Rust's retained evaluator scans real files and uses ``metadata.modified()``
    as its recency tie-break.  The logical clock remains the fallback for
    callers that only construct an in-memory file list; the evaluation harness
    materializes the same files before retrieval so its ordering observes the
    same filesystem semantics without consulting Rust output.
    """

    ordered = sorted(
        files,
        key=lambda file: (
            file.mtime_ms,
            normalize_memory_path(file.file_path),
        ),
    )
    for file in ordered:
        path = Path(file.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file.content, encoding="utf-8")
    materialized: list[RetrievedMemoryFile] = []
    for file in files:
        path = Path(file.file_path)
        try:
            mtime_ms = path.stat().st_mtime_ns // 1_000_000
        except OSError:
            mtime_ms = file.mtime_ms
        materialized.append(replace(file, mtime_ms=mtime_ms))
    return materialized


def format_progress_message(update: ProgressUpdate) -> str:
    return (
        f"sample {update.sample_index}/{update.total_samples} "
        f"{update.sample_id} q{update.question_index}/{update.sample_question_total} "
        f"overall {update.completed_cases}/{update.total_cases}"
    )


def write_harness_artifacts(
    harness_root: str | Path,
    output: EvalOutput,
    config: EvalHarnessConfig,
) -> None:
    root = Path(harness_root)
    cases_dir = root / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    traces = [_case_trace_from_score(score) for score in output.cases]
    for trace in traces:
        _write_json(cases_dir / f"{trace['case_id'].replace('::', '__')}.json", trace)

    failures = [trace for trace in traces if trace["failure_kind"] != "full_hit"]
    _write_json(
        root / "run_manifest.json",
        {
            "created_at_ms": _unix_now_ms(),
            "config": asdict(config),
            "summary": asdict(output.summary),
            "total_failures": len(failures),
        },
    )
    _write_json(root / "category_breakdown.json", summarize_scores_by_locomo_category(output.cases))
    _write_json(root / "failure_buckets.json", _summarize_failure_buckets(failures))
    _write_json(root / "failure_report.json", failures)
    (root / "failure_report.md").write_text(
        _render_failure_report_markdown(output.summary, failures),
        encoding="utf-8",
    )
    _write_json(root / "stage_profile.json", output.stage_profile)


def _parse_questions(raw_questions: list[dict[str, Any]]) -> list[LoCoMoQuestion]:
    questions: list[LoCoMoQuestion] = []
    for raw in raw_questions:
        if not isinstance(raw, dict) or not isinstance(raw.get("question"), str):
            raise ValueError("LoCoMo qa entries require a string question")
        answer = raw.get("answer")
        adversarial_answer = raw.get("adversarial_answer")
        if adversarial_answer is not None and not isinstance(adversarial_answer, str):
            raise ValueError("LoCoMo adversarial_answer must be a string or null")
        evidence = raw.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise ValueError("LoCoMo evidence must be an array of strings")
        category = raw.get("category")
        invalid_category = (
            category is not None and not isinstance(category, int)
        ) or isinstance(category, bool) or (
            isinstance(category, int) and category < 0
        )
        if invalid_category:
            raise ValueError("LoCoMo category must be a non-negative integer or null")
        questions.append(
            LoCoMoQuestion(
                question=raw["question"],
                answer=answer,
                adversarial_answer=adversarial_answer,
                evidence=list(evidence),
                category=category,
            )
        )
    return questions


def _parse_session_datetimes(conversation: dict[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for key, value in conversation.items():
        if not key.startswith("session_") or not key.endswith("_date_time"):
            continue
        number = key.removeprefix("session_").removesuffix("_date_time")
        if number.isdigit() and isinstance(value, str) and value.strip():
            result[int(number)] = value.strip()
    return dict(sorted(result.items()))


def _parse_session_summaries(raw: dict[str, str]) -> dict[int, str]:
    if not isinstance(raw, dict):
        raise ValueError("session_summary must be an object")
    result: dict[int, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("session_summary keys and values must be strings")
        suffix = key.removeprefix("session_").removesuffix("_summary")
        number = int(suffix) if key.startswith("session_") and suffix.isdigit() else None
        if number is not None:
            result[number] = value
    return dict(sorted(result.items()))


def _parse_event_summaries(raw: dict[str, Any]) -> dict[int, SessionEvents]:
    result: dict[int, SessionEvents] = {}
    for key, value in raw.items():
        number = _session_number(key)
        if number is None or not isinstance(value, dict):
            continue
        date_value = value.get("date")
        date = date_value if isinstance(date_value, str) else None
        items = {
            str(speaker): [item for item in entries if isinstance(item, str)]
            for speaker, entries in sorted(value.items())
            if speaker != "date" and isinstance(entries, list)
        }
        items = {speaker: entries for speaker, entries in items.items() if entries}
        result[number] = SessionEvents(
            date=date,
            items_by_speaker=items,
        )
    return dict(sorted(result.items()))


def _parse_observations(raw: dict[str, Any]) -> dict[int, list[ObservationNote]]:
    result: dict[int, list[ObservationNote]] = {}
    for key, value in raw.items():
        number = _session_number(key)
        if number is None or not isinstance(value, dict):
            continue
        notes: list[ObservationNote] = []
        # Rust deserializes JSON objects into ordered maps; iterate speakers in
        # lexical order so event/observation ordinals and file names match.
        for speaker, entries in sorted(value.items()):
            if not isinstance(entries, list):
                continue
            for entry in entries:
                is_pair = isinstance(entry, list) and len(entry) >= 2
                is_string_pair = (
                    is_pair and isinstance(entry[0], str) and isinstance(entry[1], str)
                )
                if is_string_pair:
                    notes.append(
                        ObservationNote(
                            speaker=str(speaker),
                            evidence_id=str(entry[1]),
                            text=str(entry[0]),
                        )
                    )
                elif isinstance(entry, dict):
                    evidence_id = entry.get("evidence_id")
                    text = entry.get("text")
                    if isinstance(evidence_id, str) and isinstance(text, str):
                        notes.append(
                            ObservationNote(
                                speaker=str(speaker),
                                evidence_id=evidence_id,
                                text=text,
                            )
                        )
        if notes:
            result[number] = notes
    return dict(sorted(result.items()))


def _sample_entity_names(
    sample: PreparedSample,
    files: list[RetrievedMemoryFile] | None = None,
) -> list[str]:
    names = {
        record.speaker.strip()
        for record in sample.records
        if record.speaker.strip()
    }
    for notes in sample.observations.values():
        names.update(note.speaker.strip() for note in notes if note.speaker.strip())
    for events in sample.event_summaries.values():
        names.update(
            speaker.strip() for speaker in events.items_by_speaker if speaker.strip()
        )
    if files is None:
        return sorted(names)
    ordered = []
    for file in sorted(
        (
            file
            for file in files
            if "/wiki/entities/" in normalize_memory_path(file.file_path)
        ),
        key=lambda file: (-file.mtime_ms, file.filename),
    ):
        speaker = Path(file.file_path).stem
        canonical_match = re.search(r"(?im)^canonical_id:\s*(.+)$", file.content)
        if canonical_match:
            canonical = canonical_match.group(1).strip()
            if canonical and canonical not in ordered:
                ordered.append(canonical)
        if speaker in names and speaker not in ordered:
            ordered.append(speaker)
    return ordered + sorted(names.difference(ordered))


def _iter_multimodal_turns(sample: PreparedSample) -> list[dict[str, Any]]:
    conversation = sample.raw_sample.get("conversation", {})
    if not isinstance(conversation, dict):
        return []
    turns: list[dict[str, Any]] = []
    session_numbers = []
    for key in conversation:
        number = _session_number(str(key))
        if number is not None:
            session_numbers.append(number)
    for session_number in sorted(session_numbers):
        session = conversation.get(f"session_{session_number}")
        if not isinstance(session, list):
            continue
        date = str(conversation.get(f"session_{session_number}_date_time", ""))
        for index, raw_turn in enumerate(session, start=1):
            if not isinstance(raw_turn, dict):
                continue
            images = raw_turn.get("img_url", [])
            if isinstance(images, str):
                images = [images] if images.strip() else []
            elif isinstance(images, list):
                images = [str(item).strip() for item in images if str(item).strip()]
            else:
                images = []
            caption = str(raw_turn.get("blip_caption", "") or "").strip()
            query = str(raw_turn.get("query", "") or "").strip()
            if not images and not caption and not query:
                continue
            evidence_id = str(raw_turn.get("dia_id") or f"D{session_number}:{index}")
            turns.append(
                {
                    "evidence_id": evidence_id,
                    "session_number": session_number,
                    "date": date,
                    "speaker": str(raw_turn.get("speaker", "")),
                    "text": str(raw_turn.get("text", "") or "").strip(),
                    "caption": caption,
                    "query": query,
                    "images": images,
                }
            )
    return turns


def _render_multimodal_memory_page(turn: dict[str, Any]) -> str:
    images = turn["images"]
    image_lines = (
        "\n".join(f"- source: {image}\n  local: (none)\n  status: skipped" for image in images)
        if images
        else "(none)"
    )
    topics = [item for item in (turn["query"], turn["caption"]) if item]
    topics_text = ", ".join(topics) if topics else "multimodal"
    sources = ", ".join(f'"{item}"' for item in images)
    if not sources:
        sources = f'"locomo_refined:{turn["evidence_id"]}"'
    return (
        "---\n"
        'type: "memory"\nmodality: "image"\n'
        f'date: "{turn["date"]}"\nupdated: "2026-04-21"\n'
        'tags: ["locomo_refined", "multimodal"]\n'
        f'aliases: ["{turn["evidence_id"]}"]\n'
        f"sources: [{sources}]\n"
        'maturity: "compiled"\n'
        f'artifact_id: "{_evidence_slug(turn["evidence_id"])}_multimodal"\n'
        f'linked_entities: ["{turn["speaker"]}"]\n'
        f'linked_topics: ["{topics_text}"]\n'
        "---\n\n"
        f"# {turn['speaker']} multimodal memory {turn['evidence_id']}\n\n"
        f"- Evidence: {turn['evidence_id']}\n"
        f"- Session: D{turn['session_number']}\n"
        f"- Speaker: {turn['speaker']}\n"
        f"- Query: {turn['query']}\n\n"
        f"## Turn Text\n{turn['text']}\n\n"
        f"## Caption\n{turn['caption'] or '(none)'}\n\n"
        f"## Images\n{image_lines}\n\n"
        "## Vision Summary\nstatus: disabled\n"
    )


def _render_retained_entity_page(sample: PreparedSample, speaker: str) -> str:
    normalized_speaker = speaker.casefold()
    records_by_session: dict[int, list[ConversationRecord]] = {}
    for record in sample.records:
        if record.speaker.casefold() != normalized_speaker:
            continue
        session_number = _record_session_number(record)
        records_by_session.setdefault(session_number, []).append(record)
    records = _select_evenly_spaced(
        [
            record
            for session_number in sorted(records_by_session)
            for record in _first_and_last(records_by_session[session_number])
        ],
        10,
    )

    observations_by_session: dict[int, list[tuple[int, ObservationNote]]] = {}
    for session_number, notes in sorted(sample.observations.items()):
        matching = [
            (ordinal, note)
            for ordinal, note in enumerate(notes, start=1)
            if note.speaker.casefold() == normalized_speaker
        ]
        if matching:
            observations_by_session[session_number] = matching
    observations = _select_evenly_spaced(
        [
            note
            for session_number in sorted(observations_by_session)
            for note in _first_and_last(observations_by_session[session_number])
        ],
        14,
    )

    events_by_session: dict[int, list[tuple[int, str]]] = {}
    for session_number, events in sorted(sample.event_summaries.items()):
        flattened = []
        ordinal = 0
        for event_speaker, entries in sorted(events.items_by_speaker.items()):
            for entry in entries:
                ordinal += 1
                if event_speaker.casefold() == normalized_speaker:
                    flattened.append((ordinal, entry))
        if flattened:
            events_by_session[session_number] = flattened
    selected_events = _select_evenly_spaced(
        [
            (session_number, ordinal, entry)
            for session_number in sorted(events_by_session)
            for ordinal, entry in _first_and_last(events_by_session[session_number])
        ],
        10,
    )

    lines = [
        "---",
        (
            f"description: Cross-session profile page for {speaker} with representative "
            "evidence-linked observations, turns, and events across sessions"
        ),
        "type: project",
        "---",
        f"# {speaker}",
        "",
    ]
    if observations:
        lines.append("## Key Observations")
        for ordinal, note in observations:
            lines.append(
                f"- [observation {note.evidence_id}]"
                f"(../observations/{_evidence_slug(note.evidence_id)}_obs_{ordinal}.md) "
                f"{_truncate_preview(note.text, 100)}"
            )
        lines.append("")
    if selected_events:
        lines.append("## Related Events")
        for session_number, ordinal, entry in selected_events:
            lines.append(
                f"- [event {ordinal}]"
                f"(../events/session_{session_number}_event_{ordinal}.md) "
                f"{_truncate_preview(entry, 100)}"
            )
        lines.append("")
    if records:
        lines.append("## Related Turns")
        for record in records:
            lines.append(
                f"- [turn {record.dia_id}]"
                f"(../turns/{_evidence_slug(record.dia_id)}.md) "
                f"{_truncate_preview(record.text, 100)}"
            )
        lines.append("")
    return "\n".join(lines)


def _record_session_number(record: ConversationRecord) -> int:
    suffix = record.session_id.removeprefix("D")
    return int(suffix) if suffix.isdigit() else 0


def _first_and_last(values: list[Any]) -> list[Any]:
    if len(values) <= 1:
        return values.copy()
    return [values[0], values[-1]]


def _select_evenly_spaced(values: list[Any], max_items: int) -> list[Any]:
    if max_items <= 0 or not values:
        return []
    if len(values) <= max_items:
        return values.copy()
    if max_items == 1:
        return values[:1]
    last_index = len(values) - 1
    return [values[(slot * last_index) // (max_items - 1)] for slot in range(max_items)]


def _coverage_from_profile_result(
    expected: list[str],
    result: Any,
) -> RetrievalCoverageSummary:
    expected_set = set(expected)
    final_hit_evidence = _intersect_evidence_hits(
        expected_set,
        result.coverage.final_file_paths,
        result.files,
    )
    candidate_hit_evidence = _intersect_evidence_hits(
        expected_set,
        result.coverage.candidate_pool_file_paths,
        result.files,
    )
    late_bridge_hit_evidence = _intersect_evidence_hits(
        expected_set,
        result.coverage.late_bridge_file_paths,
        result.files,
    )
    root_hit_evidence = _intersect_evidence_hits(
        expected_set,
        result.coverage.root_file_paths,
        result.files,
    )
    if not expected:
        miss_stage = "no_expected_evidence"
    elif set(expected) <= set(final_hit_evidence):
        miss_stage = "full_hit"
    elif candidate_hit_evidence or late_bridge_hit_evidence:
        miss_stage = "final_assembly"
    elif root_hit_evidence:
        miss_stage = "candidate_ranking"
    else:
        miss_stage = "root"
    return RetrievalCoverageSummary(
        miss_stage=miss_stage,
        root_hit_evidence=root_hit_evidence,
        candidate_pool_hit_evidence=candidate_hit_evidence,
        late_bridge_hit_evidence=late_bridge_hit_evidence,
        final_hit_evidence=final_hit_evidence,
        root_file_paths=result.coverage.root_file_paths,
        candidate_pool_file_paths=result.coverage.candidate_pool_file_paths,
        late_bridge_file_paths=result.coverage.late_bridge_file_paths,
    )


def _intersect_evidence_hits(
    expected_set: set[str],
    file_paths: list[str],
    files: list[RetrievedMemoryFile],
) -> list[str]:
    selected = set(file_paths)
    values = []
    for file in files:
        if file.file_path not in selected:
            continue
        values.extend(_extract_evidence_ids(file.content))
        if file.description:
            values.extend(_extract_evidence_ids(file.description))
    return _ordered_unique(evidence_id for evidence_id in values if evidence_id in expected_set)


def _extract_evidence_ids(text: str) -> list[str]:
    return _ordered_unique(
        match.group(0)
        for match in re.finditer(r"D\d+:\d+", text)
    )


def _extract_retrieved_evidence_ids(
    file: RetrievedMemoryFile,
    question: str,
    profile: Any,
) -> list[str]:
    # Entity pages can contain several turns.  Keep all family-related turns,
    # but for ordinary questions only count evidence attached to the most
    # question-relevant lines.  This prevents an unrelated turn in the same
    # selected page from inflating recall while preserving the Rust-compatible
    # family context behaviour.
    if _is_family_entity_question(question):
        values = _extract_evidence_ids(file.content)
    else:
        scored_lines = []
        for line in file.content.splitlines():
            evidence_ids = _extract_evidence_ids(line)
            if evidence_ids:
                scored_lines.append((score_line(line, profile, question), evidence_ids))
        scored_lines = [(score, ids) for score, ids in scored_lines if ids]
        if scored_lines:
            best_score = max(score for score, _ in scored_lines)
            values = []
            for score, ids in scored_lines:
                if score == best_score:
                    values.extend(ids)
        else:
            values = _extract_evidence_ids(file.content)
    if file.description:
        values.extend(_extract_evidence_ids(file.description))
    return _ordered_unique(values)


def _is_family_entity_question(question: str) -> bool:
    return any(
        term in question.lower()
        for term in ("kid", "kids", "child", "children", "family")
    )


def _normalize_expected_evidence(raw: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in raw:
        ids = _extract_evidence_ids(item)
        if ids:
            normalized.extend(ids)
        elif item.strip():
            normalized.append(item.strip())
    return _ordered_unique(normalized)


def _evidence_slug(evidence_id: str) -> str:
    return evidence_id.replace(":", "_")


def _truncate_preview(text: str, max_chars: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= max_chars else compact[:max_chars] + "..."


def _entity_slug(name: str) -> str:
    # Match Rust's speaker_file_name: only ASCII alphanumerics survive.
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name.strip())
    return slug.strip("_") or "entity"


def _ordered_unique(values: Any) -> list[Any]:
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _session_number(key: str) -> int | None:
    if key.startswith("events_session_"):
        suffix = key.removeprefix("events_session_")
    elif key.startswith("session_"):
        suffix = key.removeprefix("session_")
        suffix = suffix.removesuffix("_observation")
        suffix = suffix.removesuffix("_summary")
    else:
        return None
    return int(suffix) if suffix.isdigit() else None


def _normalize_refined_image_list(raw: Any) -> list[str]:
    """Normalize Rust refined-dataset ``img_url`` values without coercion."""

    if raw is None:
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if isinstance(raw, list):
        values: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError("LoCoMo_refined img_url arrays require string items")
            value = item.strip()
            if value:
                values.append(value)
        return values
    raise ValueError("LoCoMo_refined img_url must be a string, array, or null")


def _stringify_answer(question: LoCoMoQuestion) -> str:
    # Rust prefers the typed `answer` field and only falls back to
    # `adversarial_answer` when it is absent.
    if isinstance(question.answer, str):
        return question.answer
    if question.answer is not None:
        return json.dumps(question.answer, ensure_ascii=False, separators=(",", ":"))
    if question.adversarial_answer is not None:
        return question.adversarial_answer
    return ""


def _normalize_plugin_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _round4(value: float) -> float:
    # Rust's f64::round rounds halfway cases away from zero; Python's round
    # uses bankers rounding.  All retained metrics are non-negative.
    return math.floor(value * 10_000.0 + 0.5) / 10_000.0


def _sort_stage_timings(stages: list[StageTimingRecord]) -> list[StageTimingRecord]:
    return sorted(stages, key=lambda record: (-record.total_ms, record.stage))


def _macro(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _locomo_category_label(category: int) -> str | None:
    return {
        1: "1 Multi Hop",
        2: "2 Temporal",
        3: "3 Open Domain",
        4: "4 Single Hop",
        5: "5 Adversarial",
    }.get(category)


def _case_trace_from_score(score: CaseScore) -> dict[str, Any]:
    missing = sorted(set(score.expected_evidence) - set(score.hit_evidence))
    unexpected = sorted(set(score.retrieved_evidence) - set(score.expected_evidence))
    failure_kind = _failure_kind(score)
    return {
        **asdict(score),
        "missing_expected_evidence": missing,
        "unexpected_retrieved_evidence": unexpected,
        "failure_kind": failure_kind,
        "failure_bucket": failure_kind,
        "evidence_precision": f"{score.evidence_precision:.4f}",
        "evidence_recall": f"{score.evidence_recall:.4f}",
    }


def _failure_kind(score: CaseScore) -> str:
    if score.full_evidence_hit:
        return "full_hit"
    if not score.retrieved_evidence:
        return "no_retrieval"
    if not score.hit_evidence:
        return "miss"
    return "partial_hit"


def _summarize_failure_buckets(failures: list[dict[str, Any]]) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for trace in failures:
        bucket = str(trace["failure_bucket"])
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return dict(sorted(buckets.items()))


def _render_failure_report_markdown(summary: EvalSummary, failures: list[dict[str, Any]]) -> str:
    lines = [
        "# wikimem retained_eval failure report",
        "",
        f"- dataset: {summary.dataset_name}",
        f"- total_cases: {summary.total_cases}",
        f"- evidence_recall_macro: {summary.evidence_recall_macro:.4f}",
        f"- full_evidence_hit_rate: {summary.full_evidence_hit_rate:.4f}",
        f"- failures: {len(failures)}",
    ]
    for trace in failures:
        lines.extend(["", f"## {trace['case_id']}", f"- failure_kind: {trace['failure_kind']}"])
    return "\n".join(lines) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2), encoding="utf-8")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _unix_now_ms() -> int:
    return int(time.time() * 1000)
