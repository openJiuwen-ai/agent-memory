"""Selective JSONL import with stable identity, preflight and resumable server receipts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from jiuwen_memory.api import ContinuityEvent


def prepare_history(
    path: str,
    *,
    batch_id: str,
    task_id: str,
    binding_id: str,
    product: str,
    version: str,
    context_id: str,
    method: str = "user_supplied",
) -> tuple[dict, list[dict]]:
    if not all((batch_id, task_id, binding_id, product, version, context_id)):
        raise ValueError("batch, target binding and source identity are required")
    if method not in {"user_supplied", "native_export"}:
        raise ValueError("unsupported historical acquisition method")
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    events = []
    missing = set()
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no}: expected an event object")
        for key in ("occurred_at", "claimed_author"):
            if not row.get(key):
                missing.add(key)
        # Explicit target binding always wins over untrusted exported target fields.
        source_event_id = row.get("event_id")
        if source_event_id is not None:
            if not isinstance(source_event_id, str) or not source_event_id.strip():
                raise ValueError(f"line {line_no}: event_id must be a nonempty string")
            # A native identity must survive export batches and file reordering.
            identity = [product, context_id, source_event_id]
            event_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            locator = f"{product}:{context_id}:event:{source_event_id}"
        else:
            missing.add("event_id")
            # Without native identity only replay of this exact batch is provable.
            event_id = hashlib.sha256(json.dumps([batch_id, digest, line_no]).encode()).hexdigest()
            locator = f"{product}:{batch_id}:{digest}:line:{line_no}"
        event = ContinuityEvent(
            task_id=task_id,
            binding_id=binding_id,
            event_id=event_id,
            content=row["content"],
            kind=row.get("kind", "unclassified"),
            occurred_at=row.get("occurred_at"),
            claimed_author=row.get("claimed_author"),
            source_locator=locator,
            duty_ids=row.get("duty_ids", []),
        )
        events.append(asdict(event))
    manifest = dict(
        batch_id=batch_id,
        product=product,
        version=version,
        method=method,
        context_id=context_id,
        context_source="adapter",
        file_sha256=digest,
        record_count=len(events),
        missing_fields=sorted(missing),
        coverage="selected_events_only",
    )
    return manifest, events


def import_history(
    client, scope: dict, manifest: dict, events: list[dict], *, chunk_size: int = 50
) -> dict:
    """Import history through chunked ``batch_add`` calls with per-item continuity.

    Chunking keeps one HTTP request bounded (a whole history in one body is neither
    retryable nor transportable). Each chunk is sent with ``continue_on_error=False``:
    after the first non-committed item the server stops, so the remaining rows are
    **not** written and must be counted as not yet imported.
    """
    size = max(1, int(chunk_size))
    results: list[dict] = []
    imported = 0
    for start in range(0, len(events), size):
        chunk = events[start : start + size]
        items = [
            {
                "content": event["content"],
                "scope": scope,
                # 经验适用职责经 `duty:<id>` 标签承载；prepare_history 单独产出的
                # duty_ids 必须折回标签，否则导入后视图里的 duties 全丢。
                "tags": [
                    *[str(tag) for tag in event.get("tags") or []],
                    *[f"duty:{duty}" for duty in event.get("duty_ids") or []],
                ],
                "continuity": {
                    "task_id": event["task_id"],
                    "binding_id": event["binding_id"],
                    "event_id": event["event_id"],
                    "kind": event.get("kind", "unclassified"),
                    "classification_confidence": event.get(
                        "classification_confidence", "unknown"
                    ),
                    "evidence_refs": list(event.get("evidence_refs") or []),
                    "context_id": event.get("context_id", ""),
                    "source_locator": event.get("source_locator"),
                    "claimed_author": event.get("claimed_author"),
                },
            }
            for event in chunk
        ]
        outcome = client.call(
            "batch_add",
            {"items": items, "scope": scope, "continue_on_error": False},
        )
        stopped = False
        for event, item_outcome in zip(chunk, outcome.get("outcomes") or [], strict=False):
            status = item_outcome.get("continuity_status") or ""
            results.append(
                {
                    "event_id": event["event_id"],
                    "status": status or "rejected",
                    "operation_id": item_outcome.get("operation_id", ""),
                    "refs": item_outcome.get("units") or [],
                }
            )
            if status != "committed":
                stopped = True
                break
            imported += 1
        if stopped:
            # 服务端在第一个未提交项处停批，本块剩余行与后续块都还没写，不能算已导入。
            break
    return {
        "manifest": manifest,
        "results": results,
        "imported": imported,
        "remaining": len(events) - imported,
    }


def attach_existing(
    client,
    project: dict,
    *,
    task_id: str,
    expected_revision: int,
    request_id: str,
    sources: list[dict],
    required: bool = True,
) -> dict:
    """Attach old snapshots at their real Scope without moving or reindexing bodies."""
    refs = []
    for source in sources:
        unit = client.call("get", {"scope": source["scope"], "unit_id": source["unit_id"]})
        if "segments" not in unit:
            return {"status": "partial", "error": "source_unavailable"}
        segments = [
            {"content": s["content"], "assets": s.get("assets", []), "source": s["source"]}
            for s in unit["segments"]
        ]
        content_digest = hashlib.sha256(
            json.dumps(
                {"segments": segments}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        refs.append(
            {"scope": unit["scope"], "unit_id": unit["id"], "content_digest": content_digest}
        )
    return client.call(
        "continuity_task",
        {
            "scope": project,
            "request": {
                "operation": "attach",
                "task_id": task_id,
                "expected_revision": expected_revision,
                "request_id": request_id,
                "background": refs,
                "required": required,
            },
        },
    )


def main():
    import argparse
    import os

    from jiuwen_memory_adapter.continuity.client import ContinuityClient

    parser = argparse.ArgumentParser(description="Validate or selectively import continuity JSONL")
    parser.add_argument("path")
    for name in (
        "batch-id",
        "task-id",
        "binding-id",
        "product",
        "version",
        "context-id",
        "org",
        "space",
        "report",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true", help="write; default is preflight only")
    args = parser.parse_args()
    manifest, events = prepare_history(
        args.path,
        batch_id=args.batch_id,
        task_id=args.task_id,
        binding_id=args.binding_id,
        product=args.product,
        version=args.version,
        context_id=args.context_id,
    )
    report = {"manifest": manifest, "status": "validated"}
    if args.apply:
        client = ContinuityClient(
            os.environ["AGENT_MEMORY_URL"], os.environ["AGENT_MEMORY_API_KEY"]
        )
        try:
            report = import_history(
                client, {"org": args.org, "space": args.space}, manifest, events
            )
        finally:
            client.close()
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()


def backfill_relations(client, scope: dict, batch_id: str, relations: list[dict]) -> dict:
    """Publish reviewed candidates as a resumable batch; no body extraction or rewriting."""
    if not batch_id:
        raise ValueError("batch_id is required")
    results = []
    for relation in relations:
        record = {**relation, "batch_id": batch_id}
        result = client.call(
            "continuity_relation",
            {
                "scope": scope,
                "relation": record,
                "expected_revision": record.get("revision", 1) - 1,
            },
        )
        results.append(result)
        if result.get("status") == "unknown":
            break
    return {"batch_id": batch_id, "results": results, "remaining": len(relations) - len(results)}


def rollback_relations(client, scope: dict, batch_id: str, receipts: list[dict]) -> dict:
    """Revoke only the exact batch revision; concurrent later edits cause a conflict."""
    results = []
    for receipt in receipts:
        ref = receipt.get("relation_ref")
        if not ref:
            results.append({"status": "unknown"})
            continue
        unit = client.call("get", {"scope": ref["scope"], "unit_id": ref["unit_id"]})
        try:
            body = "\n".join(s["content"] for s in unit["segments"])
            record = json.loads(body)
        except (ValueError, KeyError, TypeError):
            results.append({"status": "rejected", "error": "source_unavailable"})
            continue
        if record.get("batch_id") != batch_id:
            results.append({"status": "rejected", "error": "batch_mismatch"})
            continue
        expected = record["revision"]
        record.update(revision=expected + 1, status="revoked")
        results.append(
            client.call(
                "continuity_relation",
                {"scope": scope, "relation": record, "expected_revision": expected},
            )
        )
    return {"batch_id": batch_id, "results": results}
