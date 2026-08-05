"""Rust-compatible LoCoMo_refined multimodal adjunct evaluation.

The retained evaluator builds the ordinary text wiki first.  This module then
implements the example-local Rust adjunct: one multimodal artifact per turn,
optional image materialization, optional OpenAI-compatible vision enrichment,
lexical adjunct retrieval, and post-retrieval evidence rescoring.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from common.llm.base import LLM

from evaluation.wikimem.retained_eval import (
    CaseScore,
    EvalOutput,
    PreparedSample,
    WikiMode,
    _extract_evidence_ids,
    _ordered_unique,
    prepare_locomo_samples,
    normalize_wiki_mode,
    run_retained_qmd_eval,
    summarize_scores,
    summarize_scores_by_locomo_category,
    write_harness_artifacts,
)
from evaluation.wikimem.wiki_builder import WikiBuilderMode


@dataclass(frozen=True)
class VisionEnrichmentConfig:
    api_key: str
    model: str
    base_url: str


@dataclass(frozen=True)
class MultimodalBuildOptions:
    download_images: bool = True
    request_timeout_secs: int = 8
    redownload_missing_only: bool = True
    proxy_url: str | None = None
    vision: VisionEnrichmentConfig | None = None


@dataclass(frozen=True)
class MultimodalArtifactSummary:
    artifact_count: int
    downloaded_images: int
    failed_images: int


@dataclass(frozen=True)
class MultimodalRecallHit:
    artifact_id: str
    evidence_id: str
    file_path: str
    score: float


@dataclass(frozen=True)
class OfflineExportSummary:
    export_root: str
    dataset_path: str
    manifest_path: str
    missing_images_path: str
    sample_count: int
    total_image_references: int
    downloaded_images: int
    unresolved_images: int


@dataclass(frozen=True)
class _DownloadedImageAsset:
    source_url: str
    resolved_url: str | None
    local_path: str | None
    mime_type: str | None
    download_status: str
    strategy: str | None
    error: str | None


@dataclass(frozen=True)
class _VisionSummary:
    status: str = "disabled"
    summary: str = ""
    entities: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    error: str | None = None


def build_multimodal_memory_artifacts(
    sample: PreparedSample,
    kb_root: str | Path,
    options: MultimodalBuildOptions | None = None,
) -> MultimodalArtifactSummary:
    """Build the same raw/manifest/retrieval/wiki adjunct layout as Rust."""

    options = options or MultimodalBuildOptions()
    kb_root = Path(kb_root)
    manifests_dir = kb_root / ".kb-research" / "manifests"
    retrieval_dir = kb_root / ".kb-research" / "retrieval"
    raw_dir = kb_root / "raw" / "multimodal"
    memories_dir = kb_root / "wiki" / "memories"
    assets_dir = raw_dir / "assets"
    for directory in (manifests_dir, retrieval_dir, raw_dir, memories_dir, assets_dir):
        directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        kb_root / ".wikimem-workspace.json",
        {
            "producer": "mem2.0.wikimem.python",
            "dataset_name": "locomo_refined",
            "schema_version": 1,
        },
    )

    turns = _collect_multimodal_turns(sample.raw_sample)
    downloaded_count = 0
    failed_count = 0
    for turn in turns:
        raw_path = raw_dir / f"{turn['artifact_id']}.json"
        memory_path = memories_dir / f"{turn['artifact_id']}.md"
        manifest_path = manifests_dir / f"{turn['artifact_id']}.json"
        retrieval_path = retrieval_dir / f"{turn['artifact_id']}.json"
        image_assets = _materialize_image_assets(turn, assets_dir, options)
        downloaded_count += sum(item.download_status == "downloaded" for item in image_assets)
        failed_count += sum(item.download_status == "failed" for item in image_assets)
        vision = _enrich_vision_summary(turn, image_assets, options)
        linked_topics = _collect_topics(turn, vision)
        manifest = {
            "type": "memory",
            "modality": "image",
            "date": turn["session_date"],
            "updated": "2026-04-21",
            "tags": ["locomo_refined", "multimodal"],
            "aliases": [turn["evidence_id"]],
            "sources": (
                [f"locomo_refined:{turn['evidence_id']}"]
                if not turn["images"]
                else turn["images"]
            ),
            "maturity": "compiled",
            "artifact_id": turn["artifact_id"],
            "linked_entities": [turn["speaker"]],
            "linked_topics": linked_topics,
            "title": f"{turn['speaker']} multimodal memory {turn['evidence_id']}",
            "evidence_id": turn["evidence_id"],
            "session_number": turn["session_number"],
            "speaker": turn["speaker"],
            "raw_path": str(raw_path),
            "memory_path": str(memory_path),
        }
        searchable_text = _build_searchable_text(turn, image_assets, vision)
        retrieval = {
            "artifact_id": turn["artifact_id"],
            "evidence_id": turn["evidence_id"],
            "title": manifest["title"],
            "searchable_text": searchable_text,
            "memory_path": str(memory_path),
            "vision_status": vision.status,
            "vision_summary": vision.summary or None,
            "vision_keywords": list(vision.keywords),
        }
        _write_json(
            raw_path,
            {
                "artifact_id": turn["artifact_id"],
                "evidence_id": turn["evidence_id"],
                "speaker": turn["speaker"],
                "session_number": turn["session_number"],
                "session_date": turn["session_date"],
                "text": turn["text"],
                "caption": turn["caption"],
                "query": turn["query"],
                "images": turn["images"],
                "downloaded_images": [asdict(item) for item in image_assets],
                "vision_status": vision.status,
                "vision_summary": vision.summary,
                "vision_entities": list(vision.entities),
                "vision_actions": list(vision.actions),
                "vision_attributes": list(vision.attributes),
                "vision_keywords": list(vision.keywords),
                "keywords": list(vision.keywords),
                "vision_error": vision.error,
            },
        )
        _write_json(manifest_path, manifest)
        _write_json(retrieval_path, retrieval)
        memory_path.write_text(
            _render_memory_page(manifest, turn, image_assets, vision),
            encoding="utf-8",
        )
    return MultimodalArtifactSummary(
        artifact_count=len(turns),
        downloaded_images=downloaded_count,
        failed_images=failed_count,
    )


def recall_multimodal_artifacts_for_question(
    kb_root: str | Path,
    question: str,
    top_k: int,
) -> list[MultimodalRecallHit]:
    provenance_path = Path(kb_root) / ".wikimem-workspace.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if provenance != {
        "producer": "mem2.0.wikimem.python",
        "dataset_name": "locomo_refined",
        "schema_version": 1,
    }:
        return []
    retrieval_dir = Path(kb_root) / ".kb-research" / "retrieval"
    if not retrieval_dir.exists():
        return []
    hits: list[MultimodalRecallHit] = []
    for path in sorted(retrieval_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            score = _token_score(question, str(record.get("searchable_text") or ""))
            if score > 0.0:
                hits.append(
                    MultimodalRecallHit(
                        artifact_id=str(record["artifact_id"]),
                        evidence_id=str(record["evidence_id"]),
                        file_path=str(record["memory_path"]),
                        score=score,
                    )
                )
        except (OSError, KeyError, TypeError, ValueError):
            continue
    hits.sort(key=lambda item: (-item.score, item.artifact_id))
    return hits[: max(top_k, 1)]


def rescore_case_with_multimodal_artifacts(
    baseline: CaseScore,
    adjunct_hits: list[MultimodalRecallHit],
) -> CaseScore:
    retrieved_file_paths = list(baseline.retrieved_file_paths)
    for hit in adjunct_hits:
        if hit.file_path not in retrieved_file_paths:
            retrieved_file_paths.append(hit.file_path)
    retrieved_evidence = list(baseline.retrieved_evidence)
    for path in retrieved_file_paths:
        try:
            retrieved_evidence.extend(
                _extract_evidence_ids(Path(path).read_text(encoding="utf-8"))
            )
        except OSError:
            continue
    retrieved_evidence = _ordered_unique(retrieved_evidence)
    expected = _ordered_unique(baseline.expected_evidence)
    expected_set = set(expected)
    hit_evidence = [item for item in retrieved_evidence if item in expected_set]
    retrieved_count = len(retrieved_evidence)
    hit_count = len(hit_evidence)
    expected_count = len(expected)
    return replace(
        baseline,
        expected_evidence=expected,
        retrieved_evidence=retrieved_evidence,
        hit_evidence=hit_evidence,
        retrieved_file_paths=retrieved_file_paths,
        retrieved_file_count=len(retrieved_file_paths),
        evidence_precision=hit_count / retrieved_count if retrieved_count else 0.0,
        evidence_recall=hit_count / expected_count if expected_count else 0.0,
        full_evidence_hit=bool(expected_count) and hit_count == expected_count,
    )


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
    """Run Rust's text baseline plus multimodal adjunct rescoring."""

    wiki_mode = normalize_wiki_mode(wiki_mode)
    payloads = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    samples = prepare_locomo_samples(
        payloads,
        sample_filter=sample_filter,
        include_multimodal_context=True,
    )
    output_dir = Path(output_dir)
    workspace_root = Path(workspace_root)
    baseline = run_retained_qmd_eval(
        dataset_name="locomo_refined",
        samples=samples,
        workspace_root=workspace_root,
        top_k=top_k,
        question_limit=question_limit,
        harness_root=output_dir / "harness_baseline",
        wiki_mode="text",
        llm=llm,
        wiki_builder_mode=wiki_builder_mode,
        query_llm=query_llm,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "locomo_refined_retrieval_eval_baseline.json", _eval_output_dict(baseline))

    summaries: list[MultimodalArtifactSummary] = []
    if wiki_mode == "multimodal":
        options = MultimodalBuildOptions(
            download_images=download_images,
            request_timeout_secs=request_timeout_secs,
            redownload_missing_only=redownload_missing_only,
            proxy_url=proxy_url,
            vision=vision_config,
        )
        for sample in samples:
            summaries.append(
                build_multimodal_memory_artifacts(
                    sample,
                    workspace_root / sample.sample_id,
                    options,
                )
            )

    offline_export = None
    if offline_export_root is not None:
        offline_export = export_offline_locomo_refined_dataset(
            samples=samples,
            workspace_root=workspace_root,
            export_root=offline_export_root,
        )

    final_cases: list[CaseScore] = []
    multimodal_cases_with_hits = 0
    for case in baseline.cases:
        hits = (
            recall_multimodal_artifacts_for_question(
                case.knowledge_base_root,
                case.question,
                multimodal_top_k,
            )
            if wiki_mode == "multimodal"
            else []
        )
        if hits:
            multimodal_cases_with_hits += 1
        final_cases.append(rescore_case_with_multimodal_artifacts(case, hits))
    final = EvalOutput(
        summary=summarize_scores("locomo_refined", final_cases),
        cases=final_cases,
        stage_profile=baseline.stage_profile,
    )
    _write_json(output_dir / "locomo_refined_retrieval_eval.json", _eval_output_dict(final))
    write_harness_artifacts(
        output_dir / "harness",
        final,
        config=_harness_config(
            workspace_root=workspace_root,
            top_k=top_k,
            question_limit=question_limit,
            wiki_mode=wiki_mode,
            vision_config=vision_config,
            sample_filter=sample_filter,
            wiki_builder_mode=wiki_builder_mode,
            llm=llm,
            query_llm=query_llm,
        ),
    )
    download_report_path: Path | None = None
    if wiki_mode == "multimodal":
        download_report_path = output_dir / "harness" / "download_source_report.json"
        _write_json(
            download_report_path,
            _build_download_source_report(workspace_root),
        )
    report = {
        "baseline_summary": asdict(baseline.summary),
        "final_summary": asdict(final.summary),
        "category_breakdown": [
            asdict(item) for item in summarize_scores_by_locomo_category(final.cases)
        ],
        "multimodal_artifact_count": sum(item.artifact_count for item in summaries),
        "multimodal_cases_with_hits": multimodal_cases_with_hits,
        "downloaded_images": sum(item.downloaded_images for item in summaries),
        "failed_images": sum(item.failed_images for item in summaries),
        "download_report_path": str(download_report_path) if download_report_path else None,
        "offline_export": asdict(offline_export) if offline_export else None,
        "baseline_output_path": str(output_dir / "locomo_refined_retrieval_eval_baseline.json"),
        "final_output_path": str(output_dir / "locomo_refined_retrieval_eval.json"),
        "harness_dir": str(output_dir / "harness"),
    }
    _write_json(output_dir / "harness" / "locomo_refined_run_report.json", report)
    # Keep the legacy runner contract (summary/cases/stage_profile) while
    # exposing the new multimodal run report fields to callers.
    return _jsonable(
        report
        | {
            "summary": asdict(final.summary),
            "cases": [asdict(case) for case in final.cases],
            "stage_profile": asdict(final.stage_profile),
        }
    )


def _build_download_source_report(workspace_root: Path) -> dict[str, Any]:
    """Aggregate image outcomes by source domain like Rust's report."""

    successful: dict[str, int] = defaultdict(int)
    failed: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"failed_count": 0, "example_errors": []}
    )
    for raw_path in sorted(workspace_root.glob("*/raw/multimodal/*_multimodal.json")):
        try:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in payload.get("downloaded_images") or []:
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("source_url") or "")
            domain = urllib.parse.urlparse(source_url).hostname or "(unknown)"
            status = item.get("download_status")
            if status == "downloaded":
                successful[domain] += 1
            elif status == "failed":
                entry = failed[domain]
                entry["failed_count"] += 1
                error = str(item.get("error") or "unknown_error")
                if error not in entry["example_errors"] and len(entry["example_errors"]) < 3:
                    entry["example_errors"].append(error)
    successful_domains = [
        {"domain": domain, "downloaded_count": count}
        for domain, count in sorted(successful.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    failed_domains = [
        {"domain": domain, **value}
        for domain, value in sorted(
            failed.items(), key=lambda pair: (-pair[1]["failed_count"], pair[0])
        )
    ]
    return {
        "total_downloaded_images": sum(successful.values()),
        "total_failed_images": sum(item["failed_count"] for item in failed.values()),
        "successful_domains": successful_domains,
        "failed_domains": failed_domains,
    }


def export_offline_locomo_refined_dataset(
    *,
    samples: list[PreparedSample],
    workspace_root: str | Path,
    export_root: str | Path,
) -> OfflineExportSummary:
    """Rewrite image references to a self-contained offline dataset.

    Only assets recorded by the Python-generated multimodal artifacts are
    copied. Missing or failed downloads remain as their original URLs and are
    listed in ``missing_images.json``.
    """

    workspace_root = Path(workspace_root)
    export_root = Path(export_root)
    export_root.mkdir(parents=True, exist_ok=True)
    exported_samples: list[dict[str, Any]] = []
    manifest_entries: list[dict[str, Any]] = []
    missing_entries: list[dict[str, Any]] = []
    total_image_references = 0
    downloaded_images = 0
    for sample in samples:
        sample_value = json.loads(json.dumps(sample.raw_sample, ensure_ascii=False))
        asset_map = _load_sample_asset_map(
            workspace_root / sample.sample_id / "raw" / "multimodal"
        )
        sample_total, sample_downloaded = _rewrite_sample_images_for_offline_export(
            sample_id=sample.sample_id,
            sample_value=sample_value,
            export_root=export_root,
            asset_map=asset_map,
            manifest_entries=manifest_entries,
            missing_entries=missing_entries,
        )
        total_image_references += sample_total
        downloaded_images += sample_downloaded
        exported_samples.append(sample_value)
    dataset_path = export_root / "locomo_refined_offline.json"
    manifest_path = export_root / "manifest.json"
    missing_images_path = export_root / "missing_images.json"
    _write_json(dataset_path, exported_samples)
    _write_json(manifest_path, {"entries": manifest_entries})
    _write_json(missing_images_path, {"entries": missing_entries})
    return OfflineExportSummary(
        export_root=str(export_root),
        dataset_path=str(dataset_path),
        manifest_path=str(manifest_path),
        missing_images_path=str(missing_images_path),
        sample_count=len(samples),
        total_image_references=total_image_references,
        downloaded_images=downloaded_images,
        unresolved_images=len(missing_entries),
    )


def _load_sample_asset_map(raw_dir: Path) -> dict[str, list[dict[str, Any]]]:
    asset_map: dict[str, list[dict[str, Any]]] = {}
    if not raw_dir.is_dir():
        return asset_map
    for path in sorted(raw_dir.glob("*_multimodal.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        evidence_id = payload.get("evidence_id")
        if isinstance(evidence_id, str):
            records = payload.get("downloaded_images")
            asset_map[evidence_id] = records if isinstance(records, list) else []
    return asset_map


def _rewrite_sample_images_for_offline_export(
    *,
    sample_id: str,
    sample_value: dict[str, Any],
    export_root: Path,
    asset_map: dict[str, list[dict[str, Any]]],
    manifest_entries: list[dict[str, Any]],
    missing_entries: list[dict[str, Any]],
) -> tuple[int, int]:
    conversation = sample_value.get("conversation")
    if not isinstance(conversation, dict):
        raise ValueError(f"sample {sample_id} conversation must be an object")
    session_keys = sorted(
        (
            int(key.removeprefix("session_")),
            key,
        )
        for key in conversation
        if key.startswith("session_") and key.removeprefix("session_").isdigit()
    )
    total = 0
    downloaded = 0
    for _, session_key in session_keys:
        turns = conversation.get(session_key)
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            dia_id = str(turn.get("dia_id") or "")
            if not dia_id:
                continue
            source_urls = _normalize_multimodal_image_list(turn.get("img_url"))
            if not source_urls:
                continue
            speaker = str(turn.get("speaker") or "")
            turn_text = str(turn.get("text") or "")
            records = asset_map.get(dia_id, [])
            rewritten_urls: list[str] = []
            original_urls: list[str] = []
            local_paths: list[str | None] = []
            statuses: list[str] = []
            errors: list[str | None] = []
            for image_index, source_url in enumerate(source_urls):
                total += 1
                asset = (
                    records[image_index]
                    if image_index < len(records)
                    and isinstance(records[image_index], dict)
                    else None
                )
                resolved = _resolve_offline_image(
                    sample_id=sample_id,
                    dia_id=dia_id,
                    image_index=image_index,
                    source_url=source_url,
                    asset=asset,
                    export_root=export_root,
                )
                if resolved["local_relative_path"] is not None:
                    downloaded += 1
                entry = {
                    "sample_id": sample_id,
                    "dia_id": dia_id,
                    "speaker": speaker,
                    "turn_text": turn_text,
                    "image_index": image_index,
                    "source_url": source_url,
                    "rewritten_img_url": resolved["rewritten_img_url"],
                    "local_relative_path": resolved["local_relative_path"],
                    "download_status": resolved["download_status"],
                    "error": resolved["error"],
                }
                manifest_entries.append(entry)
                if entry["local_relative_path"] is None:
                    missing_entries.append(entry)
                rewritten_urls.append(resolved["rewritten_img_url"])
                original_urls.append(source_url)
                local_paths.append(resolved["local_relative_path"])
                statuses.append(resolved["download_status"])
                errors.append(resolved["error"])
            turn["img_url"] = rewritten_urls
            turn["img_url_original"] = original_urls
            turn["img_local_path"] = local_paths
            turn["img_download_status"] = statuses
            turn["img_download_error"] = errors
    return total, downloaded


def _resolve_offline_image(
    *,
    sample_id: str,
    dia_id: str,
    image_index: int,
    source_url: str,
    asset: dict[str, Any] | None,
    export_root: Path,
) -> dict[str, Any]:
    if asset is None:
        return {
            "rewritten_img_url": source_url,
            "local_relative_path": None,
            "download_status": "missing_artifact",
            "error": "missing multimodal artifact",
        }
    local_path = asset.get("local_path")
    if isinstance(local_path, str) and local_path:
        source_path = Path(local_path)
        if source_path.exists():
            extension = source_path.suffix.lstrip(".") or "img"
            relative_path = (
                Path("images")
                / sample_id
                / f"{dia_id.replace(':', '_')}_{image_index:02d}.{extension}"
            ).as_posix()
            destination = export_root / Path(relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            return {
                "rewritten_img_url": relative_path,
                "local_relative_path": relative_path,
                "download_status": "downloaded",
                "error": None,
            }
    status = str(asset.get("download_status") or "unresolved")
    return {
        "rewritten_img_url": source_url,
        "local_relative_path": None,
        "download_status": status,
        "error": str(asset.get("error") or "local downloaded asset missing on disk"),
    }


def _collect_multimodal_turns(raw_sample: dict[str, Any]) -> list[dict[str, Any]]:
    conversation = raw_sample.get("conversation")
    if not isinstance(conversation, dict):
        raise ValueError("raw_sample.conversation must be an object")
    sessions = sorted(
        (
            int(key.removeprefix("session_")),
            value,
        )
        for key, value in conversation.items()
        if key.startswith("session_")
        and key.removeprefix("session_").isdigit()
    )
    turns: list[dict[str, Any]] = []
    for session_number, messages in sessions:
        if not isinstance(messages, list):
            continue
        date = conversation.get(f"session_{session_number}_date_time")
        session_date = date if isinstance(date, str) else ""
        for message in messages:
            if not isinstance(message, dict):
                continue
            images = _normalize_multimodal_image_list(message.get("img_url"))
            caption = message.get("blip_caption")
            query = message.get("query")
            if caption is not None and not isinstance(caption, str):
                caption = ""
            if query is not None and not isinstance(query, str):
                query = ""
            caption = (caption or "").strip()
            query = (query or "").strip()
            if not images and not caption and not query:
                continue
            evidence_id = message.get("dia_id")
            if not isinstance(evidence_id, str):
                raise ValueError("multimodal message missing dia_id")
            turns.append(
                {
                    "artifact_id": f"{evidence_id.replace(':', '_')}_multimodal",
                    "evidence_id": evidence_id,
                    "speaker": str(message.get("speaker") or ""),
                    "session_number": session_number,
                    "session_date": session_date,
                    "text": str(message.get("text") or "").strip(),
                    "caption": caption,
                    "query": query,
                    "images": images,
                }
            )
    return turns


def _normalize_multimodal_image_list(raw: Any) -> list[str]:
    """Match Rust artifact collection: ignore non-string array members."""

    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if isinstance(raw, list):
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    return []


def _materialize_image_assets(
    turn: dict[str, Any],
    assets_dir: Path,
    options: MultimodalBuildOptions,
) -> list[_DownloadedImageAsset]:
    if not options.download_images:
        return [
            _DownloadedImageAsset(url, None, None, None, "skipped", "disabled", None)
            for url in turn["images"]
        ]
    opener = _build_url_opener(options.proxy_url)
    assets: list[_DownloadedImageAsset] = []
    for index, source_url in enumerate(turn["images"]):
        local_path = _resolve_asset_path(assets_dir, turn["artifact_id"], index, source_url)
        if options.redownload_missing_only and local_path.exists():
            assets.append(
                _DownloadedImageAsset(
                    source_url,
                    source_url,
                    str(local_path),
                    _infer_mime_from_path(local_path),
                    "downloaded",
                    "cache_reuse",
                    None,
                )
            )
            continue
        if source_url.startswith("data:"):
            try:
                mime = _materialize_data_url_asset(source_url, local_path)
                assets.append(
                    _DownloadedImageAsset(
                        source_url,
                        source_url,
                        str(local_path),
                        mime,
                        "downloaded",
                        "data_url_inline",
                        None,
                    )
                )
            except Exception as exc:
                assets.append(
                    _DownloadedImageAsset(
                        source_url,
                        source_url,
                        None,
                        None,
                        "failed",
                        "data_url_inline",
                        str(exc),
                    )
                )
            continue
        last_error = "download did not start"
        last_strategy = "initial"
        last_url = source_url
        for url, strategy, browser_headers, identity in _build_download_attempt_plans(source_url):
            last_strategy, last_url = strategy, url
            try:
                mime, resolved = _execute_download_attempt(
                    opener,
                    url,
                    strategy,
                    browser_headers,
                    identity,
                    local_path,
                    source_url,
                    options.request_timeout_secs,
                )
                assets.append(
                    _DownloadedImageAsset(
                        source_url,
                        resolved,
                        str(local_path),
                        mime,
                        "downloaded",
                        strategy,
                        None,
                    )
                )
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)
        if last_error:
            assets.append(
                _DownloadedImageAsset(
                    source_url,
                    last_url,
                    None,
                    None,
                    "failed",
                    last_strategy,
                    last_error,
                )
            )
    return assets


def _build_url_opener(proxy_url: str | None, *, disable_proxy: bool = False):
    if disable_proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy_url and proxy_url.strip():
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener()


def _build_download_attempt_plans(source_url: str) -> list[tuple[str, str, bool, bool]]:
    plans: list[tuple[str, str, bool, bool]] = []
    candidates = [
        (source_url, "direct", False, False),
        (source_url, "browser_headers", True, False),
        (source_url, "browser_headers_identity", True, True),
    ]
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme == "http":
        candidates.append((urllib.parse.urlunparse(parsed._replace(scheme="https")), "scheme_swap_browser_identity", True, True))
    if parsed.hostname and parsed.hostname.lower() == "imgur.com":
        candidates.append((urllib.parse.urlunparse(parsed._replace(netloc="i.imgur.com")), "normalized_direct_browser_identity", True, True))
    if parsed.hostname and parsed.hostname.lower() == "i.redd.it":
        candidates.append(("https://www.reddit.com/media?url=" + urllib.parse.quote(source_url, safe=""), "reddit_media_browser_identity", True, True))
    seen: set[tuple[str, bool, bool]] = set()
    for item in candidates:
        key = (item[0], item[2], item[3])
        if key not in seen:
            seen.add(key)
            plans.append(item)
    return plans


def _execute_download_attempt(
    opener: Any,
    url: str,
    strategy: str,
    browser_headers: bool,
    force_identity: bool,
    local_path: Path,
    source_url: str,
    timeout_secs: int,
) -> tuple[str | None, str]:
    headers = {}
    if browser_headers:
        headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/135.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": _derive_referer(source_url),
            }
        )
    if force_identity:
        headers["Accept-Encoding"] = "identity"
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=max(timeout_secs, 1)) as response:
        status = getattr(response, "status", response.getcode())
        if status < 200 or status >= 300:
            raise RuntimeError(f"http {status}")
        data = response.read()
        mime = response.headers.get("Content-Type")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    return mime, url


def _resolve_asset_path(assets_dir: Path, artifact_id: str, index: int, source_url: str) -> Path:
    extension = Path(urllib.parse.urlparse(source_url).path).suffix.lstrip(".")
    if not extension or len(extension) > 8 or not extension.isalnum():
        extension = "img"
    return assets_dir / f"{artifact_id}_{index}.{extension}"


def _derive_referer(source_url: str) -> str:
    parsed = urllib.parse.urlparse(source_url)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else "https://www.google.com/"


def _materialize_data_url_asset(source_url: str, local_path: Path) -> str | None:
    metadata, encoded = source_url.split(",", 1)
    if not metadata.endswith(";base64"):
        raise ValueError("invalid data url: only base64 payloads are supported")
    mime = metadata.removeprefix("data:").removesuffix(";base64") or None
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(base64.b64decode(encoded))
    return mime


def _infer_mime_from_path(path: Path) -> str | None:
    return mimetypes.guess_type(path.name)[0]


def _enrich_vision_summary(
    turn: dict[str, Any],
    image_assets: list[_DownloadedImageAsset],
    options: MultimodalBuildOptions,
) -> _VisionSummary:
    if options.vision is None:
        return _VisionSummary()
    local_assets = [item for item in image_assets if item.local_path]
    if not local_assets:
        return _VisionSummary(status="skipped", error="no_local_images")
    merged_summary: list[str] = []
    entities: list[str] = []
    actions: list[str] = []
    attributes: list[str] = []
    keywords: list[str] = []
    for asset in local_assets:
        try:
            data = Path(asset.local_path).read_bytes()
            mime = asset.mime_type or "image/jpeg"
            data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            prompt = (
                "You are building a retrieval memory for a conversation benchmark. "
                "Return strict JSON with keys summary, entities, actions, attributes, keywords. "
                "Be concise and concrete.\n"
                f"Speaker: {turn['speaker']}\nTurn text: {turn['text']}\n"
                f"Caption hint: {turn['caption']}\nQuery hint: {turn['query']}"
            )
            body = {
                "model": options.vision.model,
                "stream": False,
                "temperature": 0.0,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            }
            content = _vision_request(options.vision, body, options.request_timeout_secs)
            parsed = _parse_vision_output(content)
            if parsed["summary"]:
                merged_summary.append(parsed["summary"].strip())
            entities.extend(parsed["entities"])
            actions.extend(parsed["actions"])
            attributes.extend(parsed["attributes"])
            keywords.extend(parsed["keywords"])
        except Exception as exc:
            return _VisionSummary(
                status="failed",
                summary=" | ".join(item for item in merged_summary if item),
                entities=tuple(_ordered_unique(entities)),
                actions=tuple(_ordered_unique(actions)),
                attributes=tuple(_ordered_unique(attributes)),
                keywords=tuple(_ordered_unique(keywords)),
                error=str(exc),
            )
    return _VisionSummary(
        status="enriched",
        summary=" | ".join(item for item in merged_summary if item),
        entities=tuple(_ordered_unique(entities)),
        actions=tuple(_ordered_unique(actions)),
        attributes=tuple(_ordered_unique(attributes)),
        keywords=tuple(_ordered_unique(keywords)),
    )


def _vision_request(config: VisionEnrichmentConfig, body: dict[str, Any], timeout_secs: int) -> str:
    endpoint = _normalize_openai_base_url(config.base_url)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = _build_url_opener(None, disable_proxy=True)
    with opener.open(request, timeout=max(timeout_secs, 1)) as response:
        status = getattr(response, "status", response.getcode())
        if status < 200 or status >= 300:
            raise RuntimeError(f"http {status}")
        payload = json.loads(response.read().decode("utf-8"))
    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices else None
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text")) for item in content if isinstance(item, dict) and item.get("text"))
    return ""


def _normalize_openai_base_url(base_url: str) -> str:
    trimmed = base_url.strip().rstrip("/")
    if not trimmed:
        return "https://api.openai.com/v1/chat/completions"
    parsed = urllib.parse.urlparse(trimmed)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return trimmed
    if path in {"", "/", "/v1", "/api/v1", "/v1beta", "/api/v1beta"}:
        path = "/v1/chat/completions" if path in {"", "/"} else f"{path}/chat/completions"
        return urllib.parse.urlunparse(parsed._replace(path=path))
    return trimmed


def _parse_vision_output(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if not cleaned:
        raise ValueError("empty vision content")
    for candidate in (cleaned, cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
                return {
                    "summary": parsed["summary"],
                    "entities": _string_list(parsed.get("entities")),
                    "actions": _string_list(parsed.get("actions")),
                    "attributes": _string_list(parsed.get("attributes")),
                    "keywords": _string_list(parsed.get("keywords")),
                }
        except json.JSONDecodeError:
            pass
    return {"summary": cleaned, "entities": [], "actions": [], "attributes": [], "keywords": _tokenize(cleaned)}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _collect_topics(turn: dict[str, Any], vision: _VisionSummary) -> list[str]:
    values = [turn["query"], turn["caption"], vision.summary, *vision.keywords]
    topics = _ordered_unique(item for item in values if item)
    return topics or ["multimodal"]


def _build_searchable_text(
    turn: dict[str, Any],
    image_assets: list[_DownloadedImageAsset],
    vision: _VisionSummary,
) -> str:
    local_assets = " ".join(item.local_path for item in image_assets if item.local_path)
    return "\n".join(
        [
            turn["speaker"],
            turn["text"],
            turn["caption"],
            turn["query"],
            turn["session_date"],
            " ".join(turn["images"]),
            local_assets,
            vision.summary,
            " ".join(vision.entities),
            " ".join(vision.actions),
            " ".join(vision.attributes),
            " ".join(vision.keywords),
            vision.status,
        ]
    )


def _render_memory_page(
    manifest: dict[str, Any],
    turn: dict[str, Any],
    image_assets: list[_DownloadedImageAsset],
    vision: _VisionSummary,
) -> str:
    image_lines = "(none)" if not image_assets else "\n".join(
        f"- source: {item.source_url}\n  local: {item.local_path or '(none)'}\n  status: {item.download_status}"
        for item in image_assets
    )
    if not any((vision.summary, vision.entities, vision.actions, vision.attributes, vision.keywords)):
        vision_section = f"## Vision Summary\nstatus: {vision.status}\n"
    else:
        vision_section = (
            f"## Vision Summary\nstatus: {vision.status}\nsummary: {vision.summary}\n"
            f"entities: {', '.join(vision.entities) or '(none)'}\n"
            f"actions: {', '.join(vision.actions) or '(none)'}\n"
            f"attributes: {', '.join(vision.attributes) or '(none)'}\n"
            f"keywords: {', '.join(vision.keywords) or '(none)'}\n"
        )
    quote = lambda value: str(value).replace('"', '\\"')
    sources = ", ".join(f'"{quote(item)}"' for item in manifest["sources"])
    topics = ", ".join(f'"{quote(item)}"' for item in manifest["linked_topics"])
    return (
        f'---\ntype: "{quote(manifest["type"])}"\nmodality: "{quote(manifest["modality"])}"\n'
        f'date: "{quote(manifest["date"])}"\nupdated: "{quote(manifest["updated"])}"\n'
        f'tags: ["locomo_refined", "multimodal"]\naliases: ["{quote(turn["evidence_id"])}"]\n'
        f"sources: [{sources}]\nmaturity: \"compiled\"\nartifact_id: \"{quote(turn['artifact_id'])}\"\n"
        f'linked_entities: ["{quote(turn["speaker"])}"]\nlinked_topics: [{topics}]\n---\n\n'
        f"# {manifest['title']}\n\n- Evidence: {turn['evidence_id']}\n- Session: D{turn['session_number']}\n"
        f"- Speaker: {turn['speaker']}\n- Query: {turn['query']}\n\n## Turn Text\n{turn['text']}\n\n"
        f"## Caption\n{turn['caption'] or '(none)'}\n\n## Images\n{image_lines}\n\n{vision_section}"
    )


def _token_score(query: str, text: str) -> float:
    query_tokens = _tokenize(query)
    text_tokens = set(_tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    hits = sum(token in text_tokens for token in query_tokens)
    phrase_bonus = 2.0 if query.lower() in text.lower() else 0.0
    return float(hits) + phrase_bonus if hits else 0.0


def _tokenize(text: str) -> list[str]:
    values = [item for item in re.split(r"[^0-9A-Za-z]+", text) if item]
    result = []
    for value in values:
        lower = value.lower()
        if lower.endswith("ing") and len(lower) > 5:
            lower = lower[:-3]
        elif lower.endswith("ed") and len(lower) > 4:
            lower = lower[:-2]
        elif lower.endswith("s") and len(lower) > 3:
            lower = lower[:-1]
        if len(lower) > 1:
            result.append(lower)
    return result


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _eval_output_dict(output: EvalOutput) -> dict[str, Any]:
    return {
        "summary": asdict(output.summary),
        "cases": [asdict(case) for case in output.cases],
        "stage_profile": asdict(output.stage_profile),
    }


def _harness_config(
    *,
    workspace_root: Path,
    top_k: int,
    question_limit: int | None,
    wiki_mode: str,
    vision_config: VisionEnrichmentConfig | None,
    sample_filter: set[str] | None,
    wiki_builder_mode: WikiBuilderMode = "deterministic",
    llm: LLM | None = None,
    query_llm: LLM | None = None,
):
    from evaluation.wikimem.retained_eval import EvalHarnessConfig

    return EvalHarnessConfig(
        dataset_name="locomo_refined",
        samples=",".join(sorted(sample_filter)) if sample_filter else None,
        question_limit=question_limit,
        top_k=top_k,
        workspace_root=str(workspace_root),
        llm_provider=(
            type(llm).__name__
            if llm is not None
            else type(query_llm).__name__
            if query_llm is not None
            else ("nvidia" if vision_config else None)
        ),
        retrieval_plugins=["qmd_consensus"],
        wiki_mode=wiki_mode,
        wiki_builder_mode=wiki_builder_mode,
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
