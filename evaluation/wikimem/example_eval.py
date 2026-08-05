"""Lightweight example dataset runners for wikimem migration."""

from __future__ import annotations

import json
import math
import posixpath
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from common.llm.base import LLM

from evaluation.wikimem.qmd_consensus import RetrievedMemoryFile
from evaluation.wikimem.locomo_refined import (
    VisionEnrichmentConfig,
    run_locomo_refined_offline_eval as _run_locomo_refined_multimodal_eval,
)
from evaluation.wikimem.retained_eval import WikiMode, normalize_wiki_mode
from evaluation.wikimem.wiki_builder import WikiBuilderMode
from evaluation.wikimem.retrieval_profile import retrieve_qmd_consensus_files

_EVERMEM_STOPWORDS = {
    "about",
    "after",
    "and",
    "before",
    "did",
    "during",
    "final",
    "for",
    "from",
    "had",
    "has",
    "have",
    "her",
    "his",
    "how",
    "she",
    "that",
    "the",
    "their",
    "they",
    "this",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
}

_MEM_GALLERY_STOPWORDS = {
    "answer",
    "find",
    "for",
    "from",
    "help",
    "image",
    "into",
    "matches",
    "mentions",
    "option",
    "picture",
    "please",
    "question",
    "same",
    "shown",
    "that",
    "the",
    "this",
    "using",
    "what",
    "which",
    "with",
    "your",
}

_STALE_RETRIEVAL_CASE_FIELDS = {
    "baseline_retrieved_file_paths",
    "baseline_retrieved_ids",
    "final_retrieved_file_paths",
    "final_retrieved_ids",
    "final_ranked_clues",
    "ranked_clues",
    "retrieved_clue_ids",
    "retrieved_file_paths",
}

_METRIC_LABEL_SOURCES = {
    "evermembench": "dataset_reference",
    "mem_gallery": "human_annotated_clue",
    "meta_crag": "answer_derived_proxy",
}

_WORKSPACE_PROVENANCE_FILE = ".wikimem-workspace.json"
_WORKSPACE_PRODUCER = "mem2.0.wikimem.python"


def build_mem_gallery_python_workspaces(
    *,
    dialog_root: str | Path,
    workspace_root: str | Path,
    image_root: str | Path | None = None,
    dataset_names: list[str] | None = None,
    case_limit: int | None = None,
    wiki_mode: WikiMode = "multimodal",
) -> list[dict[str, Any]]:
    """Build Mem-Gallery wiki workspaces directly from raw dialog JSON."""
    wiki_mode = normalize_wiki_mode(wiki_mode)
    dialog_root = Path(dialog_root)
    workspace_root = Path(workspace_root)
    image_root = Path(image_root) if image_root is not None else None
    selected = set(dataset_names or [])
    cases: list[dict[str, Any]] = []
    for path in sorted(dialog_root.glob("*.json")):
        dataset_name = path.stem
        if selected and dataset_name not in selected:
            continue
        entry = json.loads(path.read_text(encoding="utf-8"))
        root = workspace_root / _sanitize_mem_gallery_component(dataset_name, keep_dash=True)
        turns = _mem_gallery_turn_documents(entry, dataset_name, image_root, wiki_mode=wiki_mode)
        _write_mem_gallery_python_workspace(root, turns, wiki_mode=wiki_mode)
        for index, qa in enumerate(_mem_gallery_qas(entry), start=1):
            cases.append(
                {
                    "case_id": f"{dataset_name}::q{index}",
                    "question": str(qa.get("question") or ""),
                    "silver_evidence_ids": _normalize_mem_gallery_clue_ids(qa.get("clue") or []),
                    "knowledge_base_root": str(root),
                    "question_image_caption": str(qa.get("image_caption") or "").strip(),
                    "source_session_ids": _string_list(qa.get("session_id") or []),
                }
            )
            if case_limit is not None and len(cases) >= case_limit:
                return cases
    return cases


def build_meta_crag_python_workspaces(
    *,
    data_root: str | Path,
    workspace_root: str | Path,
    dataset_variant: str = "all",
    case_limit: int | None = None,
    wiki_mode: WikiMode = "multimodal",
) -> list[dict[str, Any]]:
    """Build Meta-CRAG wiki workspaces from raw JSON/JSONL/parquet rows."""
    wiki_mode = normalize_wiki_mode(wiki_mode)
    data_root = Path(data_root)
    workspace_root = Path(workspace_root)
    cases: list[dict[str, Any]] = []
    for variant, root in _meta_crag_variant_roots(data_root, dataset_variant):
        for row in _meta_crag_rows(root):
            for sample in _meta_crag_samples_from_row(row, variant, wiki_mode=wiki_mode):
                root = workspace_root / _sanitize_mem_gallery_component(sample["sample_id"], keep_dash=True)
                _write_meta_crag_python_workspace(root, sample, wiki_mode=wiki_mode)
                cases.append(
                    {
                        "case_id": sample["sample_id"],
                        "question": sample["question"],
                        "answer": sample["answer"],
                        # Silver labels are derived once from the authoritative
                        # multimodal artifact set; changing wiki_mode must not
                        # silently change the evaluation labels.
                        "silver_evidence_ids": list(sample["silver_evidence_ids"]),
                        "knowledge_base_root": str(root),
                    }
                )
                if case_limit is not None and len(cases) >= case_limit:
                    return cases
    return cases


def run_locomo_refined_offline_eval(
    *,
    dataset_path: str | Path,
    output_dir: str | Path,
    workspace_root: str | Path,
    top_k: int = 24,
    question_limit: int | None = None,
    wiki_mode: WikiMode = "multimodal",
    multimodal_top_k: int = 6,
    download_images: bool = True,
    request_timeout_secs: int = 8,
    redownload_missing_only: bool = True,
    proxy_url: str | None = None,
    vision_config: VisionEnrichmentConfig | None = None,
    sample_filter: set[str] | None = None,
    offline_export_root: str | Path | None = None,
    llm: LLM | None = None,
    wiki_builder_mode: WikiBuilderMode = "llm",
    query_llm: LLM | None = None,
) -> dict[str, Any]:
    return _run_locomo_refined_multimodal_eval(
        dataset_path=dataset_path,
        output_dir=output_dir,
        workspace_root=workspace_root,
        top_k=top_k,
        question_limit=question_limit,
        wiki_mode=wiki_mode,
        multimodal_top_k=multimodal_top_k,
        download_images=download_images,
        request_timeout_secs=request_timeout_secs,
        redownload_missing_only=redownload_missing_only,
        proxy_url=proxy_url,
        vision_config=vision_config,
        sample_filter=sample_filter,
        offline_export_root=offline_export_root,
        llm=llm,
        wiki_builder_mode=wiki_builder_mode,
        query_llm=query_llm,
    )


def run_filesystem_proxy_eval(
    *,
    dataset_name: str,
    cases_path: str | Path,
    output_dir: str | Path,
    top_k: int,
    base_root: str | Path | None = None,
    case_limit: int | None = None,
    allow_stale_retrieval_fields: bool = False,
    mem_gallery_dialog_root: str | Path | None = None,
    wiki_mode: WikiMode | None = None,
) -> dict[str, Any]:
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    cases = cases.get("cases", []) if isinstance(cases, dict) else cases
    if case_limit is not None:
        cases = cases[:case_limit]
    if not allow_stale_retrieval_fields:
        _reject_stale_retrieval_case_fields(cases, cases_path)
    if dataset_name == "mem_gallery" and mem_gallery_dialog_root is not None:
        _fill_mem_gallery_question_metadata(cases, Path(mem_gallery_dialog_root))
    workspace_cache: dict[str, tuple[list[RetrievedMemoryFile], dict[str, str]]] = {}
    scored = [
        _score_filesystem_case(dataset_name, case, top_k, Path(base_root) if base_root else None, workspace_cache)
        for case in cases
    ]
    result = {
        "summary": _proxy_summary(dataset_name, scored, top_k, wiki_mode=wiki_mode),
        "cases": scored,
    }
    _write_json(Path(output_dir) / f"{dataset_name}_proxy_eval.json", result)
    return result


def _reject_stale_retrieval_case_fields(
    cases: list[dict[str, Any]],
    cases_path: str | Path,
) -> None:
    for index, case in enumerate(cases):
        stale = sorted(_STALE_RETRIEVAL_CASE_FIELDS & case.keys())
        if stale:
            raise ValueError(
                f"stale retrieval fields in {cases_path} "
                f"case {case.get('case_id', index)}: {', '.join(stale)}"
            )


def _mem_gallery_qas(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("human-annotated QAs") or entry.get("human_annotated_qas") or []


def _meta_crag_variant_roots(data_root: Path, dataset_variant: str) -> list[tuple[str, Path]]:
    if dataset_variant == "all":
        return [
            ("single_turn", data_root / "single_turn" / "data"),
            ("multi_turn", data_root / "multi_turn" / "data"),
        ]
    if (data_root / "data").is_dir():
        return [(dataset_variant, data_root / "data")]
    return [(dataset_variant, data_root)]


def _meta_crag_rows(root: Path) -> list[dict[str, Any]]:
    files = sorted(path for path in root.iterdir() if path.is_file() and path.name.startswith("validation"))
    if not files:
        files = sorted(path for path in root.iterdir() if path.suffix in {".json", ".jsonl", ".parquet"})
    rows: list[dict[str, Any]] = []
    for path in files:
        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.extend(payload if isinstance(payload, list) else [payload])
        elif path.suffix == ".jsonl":
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        elif path.suffix == ".parquet":
            rows.extend(_meta_crag_parquet_rows(path))
    return rows


def _meta_crag_parquet_rows(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError("Meta-CRAG parquet input needs pyarrow; run with `uv run --with pyarrow ...`") from exc
    return [dict(row) for batch in pq.ParquetFile(path).iter_batches() for row in batch.to_pylist()]


def _meta_crag_samples_from_row(
    row: dict[str, Any],
    variant: str,
    *,
    wiki_mode: WikiMode,
) -> list[dict[str, Any]]:
    turns = row.get("turns") or {}
    queries = _string_list(turns.get("query") or [])
    searches = _string_list(turns.get("search_query") or [])
    answers = _string_list(turns.get("answer") or [])
    full_answers = _string_list((row.get("answers") or {}).get("ans_full") or [])
    session_id = str(row.get("session_id") or "")
    out = []
    all_turns = [
        {
            "turn_index": index,
            "query": query,
            "search_query": searches[index] if index < len(searches) else query,
            "answer": (answers[index] if index < len(answers) else "") or (full_answers[index] if index < len(full_answers) else ""),
        }
        for index, query in enumerate(queries)
    ]
    for turn in all_turns:
        answer = full_answers[turn["turn_index"]] if turn["turn_index"] < len(full_answers) else turn["answer"]
        raw_image = str(row.get("image_url") or "").strip()
        if not raw_image and row.get("image"):
            raw_image = "embedded-image"
        sample = {
            "sample_id": f"{session_id}::q{turn['turn_index'] + 1}",
            "session_id": session_id,
            "dataset_variant": variant,
            "question_turn_index": turn["turn_index"],
            "question": turn["query"],
            "answer": answer,
            "image_path": "" if raw_image.startswith(("http://", "https://")) else raw_image,
            "image_url": raw_image if raw_image.startswith(("http://", "https://")) else "",
            "history_turns": [item for item in all_turns if item["turn_index"] < turn["turn_index"]],
            "current_turn": turn,
        }
        authoritative_artifacts = _meta_crag_artifacts(sample, wiki_mode="multimodal")
        sample["silver_evidence_ids"] = _meta_crag_supported_ids(
            sample,
            artifacts=authoritative_artifacts,
        )
        sample["artifact_documents"] = _meta_crag_artifacts(sample, wiki_mode=wiki_mode)
        out.append(sample)
    return out


def _meta_crag_artifacts(
    sample: dict[str, Any],
    *,
    wiki_mode: WikiMode,
) -> list[dict[str, Any]]:
    artifacts = [
        {
            "artifact_id": f"turn-{turn['turn_index']}",
            "evidence_id": f"turn-{turn['turn_index']}",
            "modality": "text",
            "title": f"Turn {turn['turn_index']}",
            "text": f"Question: {turn['query']}\nSearch query: {turn['search_query']}\nAnswer: {turn['answer']}",
            "turn_index": turn["turn_index"],
            "search_query": turn["search_query"],
        }
        for turn in sample["history_turns"]
    ]
    current = sample["current_turn"]
    artifacts.append(
        {
            "artifact_id": f"turn-{current['turn_index']}",
            "evidence_id": f"turn-{current['turn_index']}",
            "modality": "text",
            "title": f"Question turn {current['turn_index']}",
            "text": f"Question: {current['query']}\nSearch query: {current['search_query']}",
            "turn_index": current["turn_index"],
            "search_query": current["search_query"],
        }
    )
    if wiki_mode == "multimodal" and (sample.get("image_path") or sample.get("image_url")):
        artifacts.append(
            {
                "artifact_id": "image-main",
                "evidence_id": "image-main",
                "modality": "image",
                "title": f"Session image for {sample['session_id']}",
                "text": f"Session image for {sample['session_id']}",
                "turn_index": None,
                "search_query": current["search_query"],
            }
        )
    return artifacts


def _write_meta_crag_python_workspace(
    root: Path,
    sample: dict[str, Any],
    *,
    wiki_mode: WikiMode,
) -> None:
    for directory in [
        root / ".kb-research" / "manifests",
        root / ".kb-research" / "retrieval",
        root / "raw" / "multimodal",
        root / "wiki" / "memories",
        root / "wiki" / "sources",
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    _write_workspace_provenance(root, "meta_crag", wiki_mode=wiki_mode)
    index_lines = ["# MEMORY", ""]
    for artifact in sample["artifact_documents"]:
        artifact_id = artifact["artifact_id"]
        evidence_id = artifact["evidence_id"]
        searchable = _meta_crag_searchable_text(artifact)
        memory_path = root / "wiki" / "memories" / f"{artifact_id}.md"
        _write_json(root / "raw" / "multimodal" / f"{artifact_id}.json", artifact)
        _write_json(
            root / ".kb-research" / "retrieval" / f"{artifact_id}.json",
            {
                "artifact_id": artifact_id,
                "evidence_id": evidence_id,
                "title": artifact["title"],
                "searchable_text": searchable,
                "memory_path": memory_path.as_posix(),
                "modality": artifact["modality"],
            },
        )
        memory_path.write_text(
            f"# {artifact['title']}\nEvidence: {evidence_id}\nevidence_id: {evidence_id}\n\n{searchable}\n",
            encoding="utf-8",
        )
        (root / "wiki" / "sources" / f"{artifact_id}.md").write_text(
            f"# Source Snapshot\n\n- title: {artifact['title']}\n- modality: {artifact['modality']}\n",
            encoding="utf-8",
        )
        index_lines.append(f"- [{artifact['title']}](wiki/memories/{artifact_id}.md)")
    (root / "MEMORY.md").write_text("\n".join(index_lines), encoding="utf-8")


def _meta_crag_supported_ids(
    sample: dict[str, Any],
    *,
    artifacts: list[dict[str, Any]] | None = None,
) -> list[str]:
    artifacts = artifacts if artifacts is not None else sample["artifact_documents"]
    answer = str(sample.get("answer") or "")
    supported = [
        artifact["evidence_id"]
        for artifact in artifacts
        if _meta_crag_answer_supported(answer, artifact)
    ]
    if supported:
        return supported
    best = max(
        artifacts,
        key=lambda artifact: _meta_crag_support_proxy_score(sample, artifact),
        default=None,
    )
    if best and _meta_crag_support_proxy_score(sample, best) > 0:
        return [best["evidence_id"]]
    return []


def _meta_crag_answer_supported(answer: str, artifact: dict[str, Any]) -> bool:
    normalized_answer = _normalize_support_text(answer)
    text = _normalize_support_text(f"{artifact['title']}\n{artifact['text']}")
    answer_tokens = _support_tokens(answer)
    return bool(normalized_answer and normalized_answer in text) or _support_tokens_match(answer_tokens, _support_tokens(text))


def _meta_crag_support_proxy_score(sample: dict[str, Any], artifact: dict[str, Any]) -> int:
    if artifact["evidence_id"] == f"turn-{sample['question_turn_index']}":
        return 0
    candidate = set(_support_tokens(f"{artifact['title']}\n{artifact['text']}"))
    answer_tokens = [token for token in _support_tokens(sample.get("answer") or "") if len(token) > 2]
    question_tokens = [token for token in _support_tokens(sample.get("question") or "") if len(token) > 2]
    score = 4 * sum(1 for token in answer_tokens if token in candidate)
    score += sum(1 for token in question_tokens if token in candidate)
    if artifact["evidence_id"] == "image-main" and (sample.get("image_path") or sample.get("image_url")):
        score += 16
    if artifact["evidence_id"].startswith("turn-"):
        score += 1
    return score


def _meta_crag_searchable_text(artifact: dict[str, Any]) -> str:
    return f"{artifact['title']}\n{artifact['text']}\nSearch query: {artifact.get('search_query') or ''}"


def _normalize_support_text(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _support_tokens(text: str) -> list[str]:
    return [token.lower() for token in re.split(r"[^0-9A-Za-z]+", text) if token]


def _support_tokens_match(answer_tokens: list[str], candidate_tokens: list[str]) -> bool:
    significant = [token for token in answer_tokens if len(token) > 2]
    candidate = set(candidate_tokens)
    return bool(significant) and all(token in candidate for token in significant)


def _mem_gallery_turn_documents(
    entry: dict[str, Any],
    dataset_name: str,
    image_root: Path | None,
    *,
    wiki_mode: WikiMode,
) -> list[dict[str, Any]]:
    speaker = str((entry.get("character_profile") or {}).get("name") or dataset_name)
    turns = []
    for session in entry.get("multi_session_dialogues") or []:
        session_id = str(session.get("session_id") or "")
        for index, dialogue in enumerate(session.get("dialogues") or [], start=1):
            clue_id = str(dialogue.get("round") or index)
            image_path = _first(dialogue.get("input_image") or []) if wiki_mode == "multimodal" else ""
            image_caption = _first(dialogue.get("image_caption") or []) if wiki_mode == "multimodal" else ""
            image_id = _first(dialogue.get("image_id") or []) if wiki_mode == "multimodal" else ""
            lines = []
            user = str(dialogue.get("user") or "").strip()
            assistant = str(dialogue.get("assistant") or "").strip()
            if user:
                lines.append(f"user ({speaker}): {user}")
            if assistant:
                lines.append(f"assistant: {assistant}")
            turns.append(
                {
                    "clue_id": clue_id,
                    "session_id": session_id,
                    "dialogue_round": index,
                    "timestamp": session.get("date"),
                    "text": "\n".join(lines),
                    "image_path": _resolve_mem_gallery_image_ref(image_root, dataset_name, image_path),
                    "image_caption": image_caption,
                    "image_id": image_id,
                }
            )
    return turns


def _write_mem_gallery_python_workspace(
    root: Path,
    turns: list[dict[str, Any]],
    *,
    wiki_mode: WikiMode,
) -> None:
    for directory in [
        root / "raw" / "sessions",
        root / "raw" / "turns",
        root / "raw" / "clue_summaries",
        root / "wiki" / "turns",
        root / "wiki" / "observations",
        root / "wiki" / "memories",
        root / ".mem-gallery",
        root / ".kb-research" / "retrieval",
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    _write_workspace_provenance(root, "mem_gallery", wiki_mode=wiki_mode)
    for session_id in sorted({turn["session_id"] for turn in turns}):
        session_turns = [turn for turn in turns if turn["session_id"] == session_id]
        (root / "raw" / "sessions" / f"{session_id}.md").write_text(
            _render_mem_gallery_session(session_id, session_turns),
            encoding="utf-8",
        )
    support = []
    for turn in turns:
        raw_slug = _mem_gallery_turn_file_slug(turn)
        evidence_slug = _evidence_slug(turn["clue_id"])
        raw_turn = _render_mem_gallery_turn_raw_artifact(turn)
        clue_summary = _render_mem_gallery_clue_summary(turn)
        searchable = _mem_gallery_turn_searchable_text(turn)
        (root / "raw" / "turns" / f"{raw_slug}.md").write_text(raw_turn, encoding="utf-8")
        (root / "raw" / "clue_summaries" / f"{raw_slug}.md").write_text(clue_summary, encoding="utf-8")
        (root / "wiki" / "turns" / f"{evidence_slug}.md").write_text(
            f"# Turn {turn['clue_id']}\nEvidence: {turn['clue_id']}\nSession: {turn['session_id']}\n\n{searchable}\n",
            encoding="utf-8",
        )
        observation_path = (
            root
            / "wiki"
            / "observations"
            / f"{evidence_slug}_obs_{_observation_suffix(turn['clue_id'])}.md"
        )
        observation_path.write_text(
            f"# Observation {turn['clue_id']}\nEvidence: {turn['clue_id']}\nSession: {turn['session_id']}\n\n{searchable}\n",
            encoding="utf-8",
        )
        memory_path = root / "wiki" / "memories" / f"clue-summary-{evidence_slug}.md"
        memory_path.write_text(
            f"# Clue summary {turn['clue_id']}\nEvidence: {turn['clue_id']}\nSession: {turn['session_id']}\n\n{clue_summary}\n",
            encoding="utf-8",
        )
        retrieval_path = root / ".kb-research" / "retrieval" / f"{evidence_slug}.json"
        _write_json(
            retrieval_path,
            {
                "artifact_id": f"turn-{evidence_slug}",
                "evidence_id": turn["clue_id"],
                "memory_path": memory_path.as_posix(),
                "title": f"Turn {turn['clue_id']}",
                "searchable_text": searchable,
            },
        )
        support.append({"memory_path": memory_path.as_posix(), "linked_clue_ids": [turn["clue_id"]]})
    _write_json(root / ".mem-gallery" / "artifact_support_map.json", support)


def _render_mem_gallery_session(session_id: str, turns: list[dict[str, Any]]) -> str:
    lines = [f"# Session {session_id}", ""]
    for turn in turns:
        lines.extend([f"## {turn['clue_id']}", str(turn["text"])])
        if turn.get("image_caption"):
            lines.append(f"Image caption: {turn['image_caption']}")
        if turn.get("image_id"):
            lines.append(f"Image id: {turn['image_id']}")
        lines.append("")
    return "\n".join(lines)


def _render_mem_gallery_turn_raw_artifact(turn: dict[str, Any]) -> str:
    lines = [f"# Turn {turn['clue_id']}", "", str(turn["text"])]
    if turn.get("image_path"):
        lines.extend(["", f"Image path: {turn['image_path']}"])
    if turn.get("image_caption"):
        lines.append(f"Image caption: {turn['image_caption']}")
    if turn.get("image_id"):
        lines.append(f"Image id: {turn['image_id']}")
    return "\n".join(lines)


def _render_mem_gallery_clue_summary(turn: dict[str, Any]) -> str:
    lines = [f"# Clue {turn['clue_id']}", "", str(turn["text"])]
    if turn.get("image_id"):
        lines.append(f"Linked image clue: {turn['image_id']}")
    if turn.get("image_caption"):
        lines.append(f"Image caption: {turn['image_caption']}")
    return "\n".join(lines)


def _mem_gallery_turn_searchable_text(turn: dict[str, Any]) -> str:
    parts = [str(turn["text"])]
    if turn.get("image_caption"):
        parts.append(f"image caption: {turn['image_caption']}")
    if turn.get("image_id"):
        parts.append(f"image id: {turn['image_id']}")
    return "\n".join(parts)


def _resolve_mem_gallery_image_ref(image_root: Path | None, dataset_name: str, raw_path: str) -> str:
    raw_path = raw_path.strip()
    if not raw_path:
        return ""
    relative = raw_path.replace("../image/", "")
    if Path(raw_path).is_absolute() or image_root is None:
        return raw_path
    candidate = image_root / relative
    if candidate.is_file():
        return candidate.as_posix()
    return (image_root / dataset_name / Path(relative).name).as_posix()


def _normalize_mem_gallery_clue_ids(values: list[Any]) -> list[str]:
    return _string_list(values)


def _mem_gallery_turn_file_slug(turn: dict[str, Any]) -> str:
    return f"D{_sanitize_mem_gallery_component(turn['session_id'])}_{turn['dialogue_round']}"


def _sanitize_mem_gallery_component(value: Any, *, keep_dash: bool = False) -> str:
    allowed = {"_"} | ({"-"} if keep_dash else set())
    return "".join(ch if ch.isascii() and (ch.isalnum() or ch in allowed) else "_" for ch in str(value))


def _evidence_slug(evidence_id: str) -> str:
    return evidence_id.replace(":", "_")


def _observation_suffix(evidence_id: str) -> str:
    return evidence_id.rsplit(":", 1)[-1] or evidence_id


def _first(values: list[Any]) -> str:
    if isinstance(values, str):
        return values
    return str(values[0]) if values else ""


def _string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [str(value).strip() for value in values if str(value).strip()]


def _fill_mem_gallery_question_metadata(cases: list[dict[str, Any]], dialog_root: Path) -> None:
    cache: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        case_id = str(case.get("case_id") or "")
        if "::q" not in case_id:
            continue
        dataset_name, question_number = case_id.rsplit("::q", 1)
        if not question_number.isdigit():
            continue
        if dataset_name not in cache:
            path = dialog_root / f"{dataset_name}.json"
            if not path.exists():
                cache[dataset_name] = []
            else:
                payload = json.loads(path.read_text(encoding="utf-8"))
                cache[dataset_name] = (
                    payload.get("human-annotated QAs")
                    or payload.get("human_annotated_qas")
                    or []
                )
        index = int(question_number) - 1
        qas = cache[dataset_name]
        if not 0 <= index < len(qas):
            continue
        qa = qas[index]
        if qa.get("image_caption") and not case.get("question_image_caption"):
            case["question_image_caption"] = qa["image_caption"]
        if qa.get("session_id") and not case.get("source_session_ids"):
            case["source_session_ids"] = qa["session_id"]


def run_evermembench_python_eval(
    *,
    data_root: str | Path,
    output_dir: str | Path,
    top_k: int = 100,
    topic_names: list[str] | None = None,
    topic_limit: int | None = None,
    question_offset: int = 0,
    question_limit: int | None = None,
    wiki_mode: WikiMode = "text",
) -> dict[str, Any]:
    wiki_mode = normalize_wiki_mode(wiki_mode)
    topics = sorted(path for path in Path(data_root).iterdir() if path.is_dir())
    if topic_names is not None:
        selected = set(topic_names)
        topics = [path for path in topics if path.name in selected]
    if topic_limit is not None:
        topics = topics[:topic_limit]
    cases = []
    for topic in topics:
        cases.extend(_score_evermem_topic(topic, top_k, question_offset, question_limit))
    result = {
        "summary": _proxy_summary("evermembench", cases, top_k, wiki_mode=wiki_mode),
        "cases": cases,
    }
    result["summary"]["effective_wiki_mode"] = "text"
    _write_json(Path(output_dir) / "evermembench_python_eval.json", result)
    return result


def _score_filesystem_case(
    dataset_name: str,
    case: dict[str, Any],
    top_k: int,
    base_root: Path | None,
    workspace_cache: dict[str, tuple[list[RetrievedMemoryFile], dict[str, str]]],
) -> dict[str, Any]:
    root = _resolve_root(case["knowledge_base_root"], base_root)
    include_retrieval_json = (
        dataset_name in {"meta_crag", "mem_gallery"}
        and _has_python_workspace_provenance(root, dataset_name)
    )
    cache_key = f"{root.as_posix()}::retrieval_json={include_retrieval_json}"
    if cache_key not in workspace_cache:
        workspace_cache[cache_key] = _read_workspace_files(
            root,
            include_retrieval_json=include_retrieval_json,
        )
    files, evidence_by_path = workspace_cache[cache_key]
    retrieved, file_paths = _retrieve_filesystem_evidence(
        dataset_name,
        case,
        files,
        evidence_by_path,
        top_k,
    )
    expected = _case_expected_ids(case)
    hits = [item for item in retrieved if item in set(expected)]
    return _case_score(
        dataset_name=dataset_name,
        case_id=str(case.get("case_id") or case.get("sample_id") or len(retrieved)),
        question=str(case["question"]),
        expected=expected,
        retrieved=retrieved,
        hits=hits,
        file_paths=file_paths,
        top_k=top_k,
    )


def _score_evermem_topic(
    topic: Path,
    top_k: int,
    question_offset: int,
    question_limit: int | None,
) -> list[dict[str, Any]]:
    topic_id = topic.name
    profiles = _evermem_profiles(topic.parent)
    rows = _evermem_rows(
        topic_id,
        json.loads((topic / "dialogue.json").read_text(encoding="utf-8")),
        profiles,
    )
    files = [
        RetrievedMemoryFile(
            filename=f"{_slug(row['evidence_id'])}.md",
            file_path=f"/evermembench/{topic_id}/wiki/turns/{row['session_id']}_{row['message_index']}.md",
            mtime_ms=index + 1,
            content=(
                f"# Turn {row['evidence_id']}\n"
                f"Evidence: {row['evidence_id']}\n"
                f"Session: {row['session_id']}\n"
                f"Speaker: {row['speaker']}\n"
                f"Date: {row['date']}\n"
                f"Group: {row['group']}\n\n"
                f"{row['profile']}\n\n"
                f"{row['text']}\n"
            ),
        )
        for index, row in enumerate(rows)
    ]
    evidence_by_path = {file.file_path: row["evidence_id"] for file, row in zip(files, rows)}
    token_by_path = {file.file_path: set(_content_keywords(file.content)) for file in files}
    idf_by_token = _idf_by_token(token_by_path.values())
    session_by_ref = {
        (row["date"], row["group"]): row["session_id"]
        for row in rows
    }
    questions = json.loads((topic / f"qa_{topic_id}.json").read_text(encoding="utf-8"))
    if question_offset:
        questions = questions[question_offset:]
    if question_limit is not None:
        questions = questions[:question_limit]
    cases = []
    for question in questions:
        retrieved, file_paths = _retrieve_evermem_evidence(
            str(question["Q"]),
            files=files,
            evidence_by_path=evidence_by_path,
            rows=rows,
            token_by_path=token_by_path,
            idf_by_token=idf_by_token,
            top_k=top_k,
        )
        expected = _evermem_expected(question.get("R") or [], session_by_ref)
        hits = [item for item in retrieved if item in set(expected)]
        cases.append(
            _case_score(
                dataset_name="evermembench",
                case_id=f"{topic_id}::{question.get('id')}",
                question=str(question["Q"]),
                expected=expected,
                retrieved=retrieved,
                hits=hits,
                file_paths=file_paths,
                top_k=top_k,
            )
        )
    return cases


def _read_workspace_files(
    root: Path,
    *,
    include_retrieval_json: bool = False,
) -> tuple[list[RetrievedMemoryFile], dict[str, str]]:
    files = []
    evidence_by_path = {}
    index = 0
    for path in sorted(root.rglob("*.md")):
        content = path.read_text(encoding="utf-8", errors="ignore")
        file_path = path.as_posix()
        index += 1
        files.append(
            RetrievedMemoryFile(
                filename=path.name,
                file_path=file_path,
                mtime_ms=index,
                content=content,
            )
        )
        content_ids = _content_evidence_ids(content)
        evidence_by_path[file_path] = (
            content_ids[0] if content_ids else _evidence_id_from_path(path)
        )
    _merge_mem_gallery_support_map(root, evidence_by_path)
    retrieval_paths = sorted((root / ".kb-research" / "retrieval").glob("*.json"))
    for path in (retrieval_paths if include_retrieval_json else []):
        record = json.loads(path.read_text(encoding="utf-8"))
        evidence_id = str(record.get("evidence_id") or "").strip()
        if not evidence_id:
            memory_path = str(record.get("memory_path") or "").replace("\\", "/")
            evidence_id = evidence_by_path.get(memory_path, "")
        if not evidence_id:
            continue
        title = str(record.get("title") or "")
        searchable_text = str(record.get("searchable_text") or "")
        artifact_id = str(record.get("artifact_id") or path.stem)
        file_path = path.as_posix()
        index += 1
        files.append(
            RetrievedMemoryFile(
                filename=path.name,
                file_path=file_path,
                mtime_ms=index,
                content=(
                    f"# {title or artifact_id}\n"
                    f"Artifact: {artifact_id}\n"
                    f"Evidence: {evidence_id}\n\n"
                    f"{searchable_text}\n"
                ),
            )
        )
        evidence_by_path[file_path] = evidence_id
    return files, evidence_by_path


def _write_workspace_provenance(
    root: Path,
    dataset_name: str,
    *,
    wiki_mode: WikiMode | None = None,
) -> None:
    provenance = {
        "producer": _WORKSPACE_PRODUCER,
        "dataset_name": dataset_name,
        "schema_version": 1,
    }
    if wiki_mode is not None:
        provenance["wiki_mode"] = wiki_mode
    _write_json(
        root / _WORKSPACE_PROVENANCE_FILE,
        provenance,
    )


def _has_python_workspace_provenance(root: Path, dataset_name: str) -> bool:
    path = root / _WORKSPACE_PROVENANCE_FILE
    if not path.is_file():
        return False
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return (
        provenance.get("producer") == _WORKSPACE_PRODUCER
        and provenance.get("dataset_name") == dataset_name
        and provenance.get("schema_version") == 1
    )


def _merge_mem_gallery_support_map(root: Path, evidence_by_path: dict[str, str]) -> None:
    path = root / ".mem-gallery" / "artifact_support_map.json"
    if not path.exists():
        return
    for item in json.loads(path.read_text(encoding="utf-8")):
        if not isinstance(item, dict):
            continue
        clue_ids = [str(clue_id) for clue_id in item.get("linked_clue_ids") or [] if clue_id]
        memory_path = str(item.get("memory_path") or "")
        if not clue_ids or not memory_path:
            continue
        evidence_by_path[memory_path.replace("\\", "/")] = clue_ids[0]
        marker = "/wiki/"
        if marker in memory_path:
            evidence_by_path[(root / ("wiki/" + memory_path.split(marker, 1)[1])).as_posix()] = clue_ids[0]


def _ids_for_file(
    file: RetrievedMemoryFile,
    evidence_by_path: dict[str, str],
) -> list[str]:
    if "/wiki/memories/clue-summary-" in file.file_path.replace("\\", "/") and file.file_path in evidence_by_path:
        return [evidence_by_path[file.file_path]]
    ids = _content_evidence_ids(file.content)
    if ids:
        return ids

    parent = PurePosixPath(file.file_path).parent
    ids = _ordered_unique(
        evidence_by_path.get(
            posixpath.normpath((parent / match.group(1).split("#", 1)[0]).as_posix()),
            "",
        )
        for match in re.finditer(r"\[[^\]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)", file.content)
    )
    return ids or [evidence_by_path[file.file_path]]


def _retrieve_filesystem_evidence(
    dataset_name: str,
    case: dict[str, Any],
    files: list[RetrievedMemoryFile],
    evidence_by_path: dict[str, str],
    top_k: int,
) -> tuple[list[str], list[str]]:
    query = str(case["question"])
    result = retrieve_qmd_consensus_files(
        question=query,
        files=files,
        root_files=[file for file in files if file.file_path.endswith("/MEMORY.md")],
        top_k=top_k,
    )
    retrieved = _ordered_unique(
        evidence_id
        for file in result.files
        for evidence_id in _ids_for_file(file, evidence_by_path)
    )
    file_paths = [file.file_path for file in result.files]
    if dataset_name == "meta_crag":
        adjunct_ids, adjunct_paths = _retrieve_overlap_evidence(
            query,
            files,
            evidence_by_path,
            top_k,
        )
        selected_paths = _ordered_unique(file_paths + adjunct_paths)[: max(top_k, 1)]
        return (
            _meta_crag_ids_for_paths(selected_paths, evidence_by_path),
            selected_paths,
        )
    if dataset_name == "mem_gallery":
        artifact_query = _case_query_text(dataset_name, case)
        retrieved = [
            evidence_id
            for evidence_id in retrieved
            if _is_mem_gallery_clue_id(evidence_id)
        ]
        retrieved = _rrf_fuse_ids(
            retrieved,
            _retrieve_mem_gallery_artifact_ids(
                artifact_query,
                files,
                evidence_by_path,
                top_k,
            ),
            top_k,
        )
    return retrieved, file_paths


def _case_query_text(dataset_name: str, case: dict[str, Any]) -> str:
    query = str(case["question"])
    if dataset_name != "mem_gallery":
        return query
    lines = [query]
    caption = str(case.get("question_image_caption") or case.get("image_caption") or "").strip()
    if caption:
        lines.append(f"question image caption: {caption}")
    return "\n".join(lines)


def _meta_crag_ids_for_paths(
    paths: list[str],
    evidence_by_path: dict[str, str],
) -> list[str]:
    selected = []
    for path in paths:
        normalized = path.replace("\\", "/")
        evidence_id = evidence_by_path.get(path) or evidence_by_path.get(normalized) or ""
        if "example/meta-crag/" in normalized and "/wiki/memories/" not in normalized and "/.kb-research/" not in normalized:
            evidence_id = _rust_stable_path_id(_rust_relative_meta_crag_path(normalized))
        selected.append(evidence_id)
    return _ordered_unique(selected)


def _is_mem_gallery_clue_id(evidence_id: str) -> bool:
    return re.match(r"^D\d+:(?:\d+|IMG_\d+)$", evidence_id) is not None


def _retrieve_mem_gallery_artifact_ids(
    question: str,
    files: list[RetrievedMemoryFile],
    evidence_by_path: dict[str, str],
    top_k: int,
) -> list[str]:
    query_tokens = {
        token
        for token in _content_keywords(question)
        if token not in _MEM_GALLERY_STOPWORDS
    }
    if not query_tokens:
        return []
    candidates = [
        file
        for file in files
        if "/.kb-research/retrieval/" in file.file_path.replace("\\", "/")
        and _is_mem_gallery_clue_id(evidence_by_path.get(file.file_path, ""))
    ]
    token_by_path = {
        file.file_path: set(_cached_content_keywords(file.content))
        for file in candidates
    }
    idf_by_token = _idf_by_token(token_by_path.values())
    ranked = []
    for file in candidates:
        evidence_id = evidence_by_path.get(file.file_path, "")
        score = sum(
            idf_by_token.get(token, 1.0) ** 1.35
            for token in query_tokens & token_by_path[file.file_path]
        )
        if score:
            ranked.append((-score, file.file_path, evidence_id))
    return _ordered_unique(evidence_id for _, _, evidence_id in sorted(ranked))[: max(top_k * 9, 1)]


@lru_cache(maxsize=16384)
def _cached_content_keywords(text: str) -> tuple[str, ...]:
    return tuple(_content_keywords(text))


def _rrf_fuse_ids(primary: list[str], secondary: list[str], top_k: int) -> list[str]:
    scores: dict[str, float] = {}
    for rank, evidence_id in enumerate(primary, start=1):
        scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (60 + rank)
    for rank, evidence_id in enumerate(secondary, start=1):
        scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (60 + rank)
    return [
        evidence_id
        for evidence_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ][: max(top_k, 1)]


def _rust_relative_meta_crag_path(path: str) -> str:
    marker = "example/meta-crag/"
    return marker + path.split(marker, 1)[1] if marker in path else path


def _rust_stable_path_id(path: str) -> str:
    data = path.encode("utf-8") + b"\xff"
    mask = (1 << 64) - 1
    v0 = 0x736F6D6570736575
    v1 = 0x646F72616E646F6D
    v2 = 0x6C7967656E657261
    v3 = 0x7465646279746573

    def rotl(value: int, bits: int) -> int:
        return ((value << bits) & mask) | (value >> (64 - bits))

    def sip_round() -> None:
        nonlocal v0, v1, v2, v3
        v0 = (v0 + v1) & mask
        v1 = rotl(v1, 13)
        v1 ^= v0
        v0 = rotl(v0, 32)
        v2 = (v2 + v3) & mask
        v3 = rotl(v3, 16)
        v3 ^= v2
        v0 = (v0 + v3) & mask
        v3 = rotl(v3, 21)
        v3 ^= v0
        v2 = (v2 + v1) & mask
        v1 = rotl(v1, 17)
        v1 ^= v2
        v2 = rotl(v2, 32)

    end = len(data) - (len(data) % 8)
    for offset in range(0, end, 8):
        chunk = int.from_bytes(data[offset : offset + 8], "little")
        v3 ^= chunk
        sip_round()
        v0 ^= chunk
    tail = data[end:]
    last = len(data) << 56
    for index, byte in enumerate(tail):
        last |= byte << (8 * index)
    v3 ^= last
    sip_round()
    v0 ^= last
    v2 ^= 0xFF
    for _ in range(3):
        sip_round()
    return f"baseline::{(v0 ^ v1 ^ v2 ^ v3) & mask:016x}"


def _retrieve_overlap_evidence(
    query: str,
    files: list[RetrievedMemoryFile],
    evidence_by_path: dict[str, str],
    top_k: int,
) -> tuple[list[str], list[str]]:
    query_tokens = set(_query_keywords(query))
    if not query_tokens:
        return [], []
    score_by_id: dict[str, int] = {}
    path_by_id: dict[str, str] = {}
    best_score_by_id: dict[str, int] = {}
    for file in files:
        content_tokens = set(_query_keywords(file.content))
        score = len(query_tokens & content_tokens)
        if score <= 0:
            continue
        for evidence_id in _ids_for_file(file, evidence_by_path):
            score_by_id[evidence_id] = score_by_id.get(evidence_id, 0) + score
            if score > best_score_by_id.get(evidence_id, -1):
                best_score_by_id[evidence_id] = score
                path_by_id[evidence_id] = file.file_path
    ranked = sorted(score_by_id, key=lambda item: (-score_by_id[item], item))
    retrieved = ranked[: max(top_k, 1)]
    return retrieved, [path_by_id[item] for item in retrieved if item in path_by_id]


def _retrieve_evermem_evidence(
    question: str,
    *,
    files: list[RetrievedMemoryFile],
    evidence_by_path: dict[str, str],
    rows: list[dict[str, str]],
    token_by_path: dict[str, set[str]] | None = None,
    idf_by_token: dict[str, float] | None = None,
    top_k: int,
) -> tuple[list[str], list[str]]:
    scored_files = _rank_files_by_overlap(
        question,
        files,
        token_by_path=token_by_path,
        idf_by_token=idf_by_token,
    )
    index_by_id = {row["evidence_id"]: index for index, row in enumerate(rows)}
    file_by_id = {
        evidence_by_path[file.file_path]: file
        for file in files
        if file.file_path in evidence_by_path
    }
    candidate_scores: dict[str, float] = {}
    session_scores: dict[str, float] = {}
    pool_limit = max(top_k * 3, 256)
    for rank, (score, file) in enumerate(scored_files[:pool_limit], start=1):
        evidence_id = evidence_by_path.get(file.file_path)
        if not evidence_id:
            continue
        index = index_by_id.get(evidence_id)
        if index is None:
            continue
        session_id = rows[index]["session_id"]
        session_scores[session_id] = max(session_scores.get(session_id, 0.0), score)
        candidate_scores[evidence_id] = max(
            candidate_scores.get(evidence_id, 0.0),
            score + 1 / (60 + rank),
        )
        for distance, neighbor in _evermem_dialogue_neighbors(rows, index, radius=5):
            neighbor_score = score * 0.92 - distance * 0.08
            candidate_scores[neighbor["evidence_id"]] = max(
                candidate_scores.get(neighbor["evidence_id"], 0.0),
                neighbor_score,
            )
    for row in rows:
        score = session_scores.get(row["session_id"])
        if score:
            candidate_scores[row["evidence_id"]] = max(
                candidate_scores.get(row["evidence_id"], 0.0),
                score * 0.45,
            )
    selected = [
        evidence_id
        for evidence_id, _ in sorted(
            candidate_scores.items(),
            key=lambda item: (-item[1], item[0]),
        )[: max(top_k, 1)]
    ]
    paths = [
        file_by_id[evidence_id].file_path
        for evidence_id in selected
        if evidence_id in file_by_id
    ]
    return selected, paths


def _rank_files_by_overlap(
    query: str,
    files: list[RetrievedMemoryFile],
    token_by_path: dict[str, set[str]] | None = None,
    idf_by_token: dict[str, float] | None = None,
) -> list[tuple[float, RetrievedMemoryFile]]:
    query_tokens = {
        token
        for token in _content_keywords(query)
        if token not in _EVERMEM_STOPWORDS
    }
    if not query_tokens:
        return []
    scored = []
    for file in files:
        content_tokens = (
            token_by_path[file.file_path]
            if token_by_path is not None
            else set(_content_keywords(file.content))
        )
        score = sum(
            (idf_by_token.get(token, 1.0) if idf_by_token else 1.0) ** 1.35
            for token in query_tokens & content_tokens
        )
        if score > 0:
            scored.append((score, file))
    scored.sort(key=lambda item: (-item[0], item[1].file_path))
    return scored


def _idf_by_token(token_sets) -> dict[str, float]:
    token_sets = list(token_sets)
    total = len(token_sets)
    document_frequency: dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    return {
        token: math.log((total + 1) / (frequency + 1)) + 1
        for token, frequency in document_frequency.items()
    }


def _evermem_dialogue_neighbors(
    rows: list[dict[str, str]],
    index: int,
    *,
    radius: int,
) -> list[tuple[int, dict[str, str]]]:
    row = rows[index]
    neighbors = []
    for distance in range(1, radius + 1):
        for neighbor_index in (index - distance, index + distance):
            if not 0 <= neighbor_index < len(rows):
                continue
            neighbor = rows[neighbor_index]
            if neighbor["session_id"] == row["session_id"]:
                neighbors.append((distance, neighbor))
    return neighbors


def _query_keywords(text: str) -> list[str]:
    return _text_keywords(text, limit=18)


def _content_keywords(text: str) -> list[str]:
    return _text_keywords(text, limit=None)


def _text_keywords(text: str, *, limit: int | None) -> list[str]:
    keywords = _ordered_unique(
        token.lower()
        for token in re.split(r"[^0-9A-Za-z]+", text)
        if len(token) >= 3
    )
    return keywords[:limit] if limit is not None else keywords


def _content_evidence_ids(content: str) -> list[str]:
    explicit_ids = [
        _clean_evidence_id(match.group(1))
        for match in re.finditer(r"(?im)^(?:evidence|evidence_id):\s*(.+)$", content)
    ]
    clue_ids = [
        f"{match.group(2)}:{match.group(3)}"
        for match in re.finditer(r"\bclue:([^:\s\"']+):([^:\s\"']+):([^:\s\"']+)", content)
    ]
    return _ordered_unique(explicit_ids + clue_ids)


def _clean_evidence_id(value: str) -> str:
    return value.strip().strip("'\"")


def _case_expected_ids(case: dict[str, Any]) -> list[str]:
    for key in ("silver_evidence_ids", "gold_clue_ids", "expected_evidence_ids"):
        if case.get(key):
            return [str(item) for item in case[key]]
    return []


def _proxy_summary(
    dataset_name: str,
    cases: list[dict[str, Any]],
    top_k: int,
    *,
    wiki_mode: WikiMode | None = None,
) -> dict[str, Any]:
    metric_cases = (
        [case for case in cases if case["expected_evidence_ids"]]
        if dataset_name == "meta_crag"
        else cases
    )
    summary = {
        "dataset_name": dataset_name,
        "metric_label_source": _METRIC_LABEL_SOURCES.get(dataset_name, "unspecified_proxy"),
        "total_cases": len(cases),
        "evidence_labeled_cases": len(metric_cases),
        "top_k": top_k,
        "precision_at_k": _round(_mean(case["precision_at_k"] for case in metric_cases)),
        "recall_at_k": _round(_mean(case["recall_at_k"] for case in metric_cases)),
        "hitrate_at_k": _round(
            _mean(1.0 if case["hit_evidence_ids"] else 0.0 for case in metric_cases)
        ),
    }
    if wiki_mode is not None:
        summary["wiki_mode"] = normalize_wiki_mode(wiki_mode)
    return summary


def _case_score(
    *,
    dataset_name: str,
    case_id: str,
    question: str,
    expected: list[str],
    retrieved: list[str],
    hits: list[str],
    file_paths: list[str],
    top_k: int,
) -> dict[str, Any]:
    return {
        "dataset_name": dataset_name,
        "case_id": case_id,
        "question": question,
        "expected_evidence_ids": expected,
        "retrieved_evidence_ids": retrieved,
        "hit_evidence_ids": hits,
        "retrieved_file_paths": file_paths,
        "top_k": top_k,
        "precision_at_k": _round(len(hits) / len(retrieved)) if retrieved else 0.0,
        "recall_at_k": _round(len(hits) / len(expected)) if expected else 0.0,
    }


def _evermem_profiles(data_root: Path) -> dict[str, str]:
    path = data_root / "profiles.json"
    if not path.exists():
        return {}
    profiles = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(profile.get("Name", "")): _render_evermem_profile(profile)
        for profile in profiles
        if profile.get("Name")
    }


def _render_evermem_profile(profile: dict[str, Any]) -> str:
    skills = ", ".join(
        str(item.get("skill", ""))
        for item in profile.get("Skills_List", [])
        if isinstance(item, dict) and item.get("skill")
    )
    interests = ", ".join(str(item) for item in profile.get("Interests", []) if item)
    fields = [
        ("Profile", profile.get("Name")),
        ("Dept", profile.get("Dept")),
        ("Title", profile.get("Title")),
        ("Major", profile.get("Major")),
        ("Skills", skills),
        ("Interests", interests),
    ]
    return "\n".join(f"{key}: {value}" for key, value in fields if value)


def _evermem_rows(
    topic_id: str,
    days: list[dict[str, Any]],
    profiles: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    rows = []
    session_number = 0
    for day in days:
        date = str(day["date"])
        for group in sorted(day.get("dialogues", {})):
            messages = day.get("dialogues", {}).get(group) or []
            if not messages:
                continue
            session_number += 1
            session_id = f"D{session_number}"
            for message in messages or []:
                index = str(message["message_index"])
                speaker = str(message.get("speaker", ""))
                rows.append(
                    {
                        "evidence_id": f"{session_id}:{index}",
                        "session_id": session_id,
                        "message_index": index,
                        "date": date,
                        "group": str(group),
                        "speaker": speaker,
                        "profile": (profiles or {}).get(speaker, ""),
                        "text": str(message.get("dialogue", "")),
                    }
                )
    return rows


def _evermem_expected(
    refs: list[dict[str, Any]],
    session_by_ref: dict[tuple[str, str], str],
) -> list[str]:
    ids = []
    for ref in refs:
        session_id = session_by_ref.get((str(ref["date"]), str(ref["group"])))
        if not session_id:
            continue
        for index in _expand_indices(str(ref["message_index"])):
            ids.append(f"{session_id}:{index}")
    return _ordered_unique(ids)


def _expand_indices(raw: str) -> list[str]:
    values = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            start, end = [int(item.strip()) for item in part.split("-", 1)]
            values.extend(str(item) for item in range(start, end + 1))
        elif part:
            values.append(str(int(part)))
    return values


def _evidence_id_from_path(path: Path) -> str:
    stem = path.stem
    match = re.match(r"^(D\d+)_(\d+)", stem, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}:{match.group(2)}"
    return stem


def _resolve_root(raw: str, base_root: Path | None) -> Path:
    path = Path(raw)
    if path.exists() or base_root is None or path.is_absolute():
        return path
    return base_root / raw


def _ordered_unique(items) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _round(value: float) -> float:
    return round(value, 4)


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").lower()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
