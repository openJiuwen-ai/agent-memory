#!/usr/bin/env python3
"""One-off migration: relation truth MemoryUnits → Registry KV relation heads.

Before the 2026-11-05 design revision a relation's truth was a MemoryUnit and the
Registry head only stored ``ref``/``history``. The target design keeps the truth in the
Registry KV head (``{revision, digest, payload_digest, status, connector, current}``) plus
immutable ``<relation_id>/rev/<n>`` records and rebuilds the graph projection from it.

The migration:

1. claims the Registry single-writer lock (a live service makes it fail fast, so two
   writers never touch the same heads);
2. rewrites every legacy ``ref``-style head into the KV shape, carrying each historical
   revision from ``history`` into ``<relation_id>/rev/<n>``;
3. re-projects the edges from the migrated truth.

It is idempotent (heads already carrying ``current`` are skipped) and refuses to run
against a document-mode deployment. Any failure is reported per relation and the process
exits non-zero; re-running resumes from the unmigrated heads.

Usage::

    python scripts/migrate_relations_to_kv.py --config examples/role_memory/server.yaml
    python scripts/migrate_relations_to_kv.py --config server.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from jiuwen_memory.api import assemble_maintenance_ports
from jiuwen_memory.common.type_def import MemoryUnit, Segment
from jiuwen_memory.common.type_def.continuity import (
    MemoryRef,
    RelationRecord,
    canonical,
    fingerprint,
)
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
from jiuwen_memory.storage.types import IndexWriteMode

logger = logging.getLogger("migrate_relations_to_kv")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="server YAML containing continuity config")
    parser.add_argument(
        "--write",
        action="store_true",
        help="apply the migration; without it the run only reports what would change",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="explicitly request the default report-only mode"
    )
    parser.add_argument(
        "--allow-incomplete-history",
        action="store_true",
        help="migrate the head even when a historical revision cannot be read "
        "(otherwise the legacy head is kept so a later run can retry)",
    )
    return parser.parse_args(argv)


def _load_relation(memory_kv, ref: dict) -> RelationRecord:
    """Read and parse the relation stored in the legacy MemoryUnit referenced by ``ref``."""
    link = MemoryRef.parse(ref)
    raw = memory_kv.get(link.scope, memory_key(link.unit_id))
    unit = loads(raw)
    if unit is None:
        raise ValueError(f"legacy relation unit {link.unit_id} is unreadable")
    return RelationRecord.parse(json.loads(unit.content))


def _migrate_head(memory_kv, head: dict) -> tuple[str, dict, list[tuple[int, dict]]]:
    """Build the KV head plus the historical revisions it must carry."""
    relation = _load_relation(memory_kv, head["ref"])
    current = asdict(relation)
    new_head = {
        "revision": relation.revision,
        "digest": head.get("digest") or fingerprint(current),
        "status": "projecting",
        "connector": head.get("connector", ""),
        "payload_digest": fingerprint(current),
        "current": current,
    }
    history: list[tuple[int, dict]] = []
    missing: list[str] = []
    for ref in head.get("history") or []:
        try:
            previous = _load_relation(memory_kv, ref)
        except Exception as exc:
            # 历史缺失必须显式上报：静默跳过会让迁移「成功」却丢修订。
            missing.append(f"{type(exc).__name__}: {exc}")
            continue
        if previous.revision < relation.revision:
            history.append((previous.revision, asdict(previous)))
    return relation.relation_id, new_head, history, missing


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv or sys.argv[1:])
    data = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    continuity = (data.get("continuity") or {}).get("default") or {}
    params = continuity.get("params") or {}
    mode = params.get("mode")
    if mode not in {"continuity", "role"}:
        logger.error("continuity.default.params.mode is required (continuity|role)")
        return 2
    if (data.get("globals") or {}).get("write_document"):
        logger.error("relation migration is not applicable to document mode")
        return 2

    # 复用正常装配：一次装配同时给出两个 KV、**同一份**装配上下文与关闭句柄。
    # 不再自己拼第二个 AssemblyContext（那会让投影拿到另一个 GraphStore 实例）。
    try:
        ports = assemble_maintenance_ports(
            data, registry_store=params.get("registry_store", "default")
        )
    except Exception as exc:
        logger.error("cannot assemble from %s: %s: %s", args.config, type(exc).__name__, exc)
        return 2
    memory_kv, registry_kv = ports.truth_kv, ports.registry_kv
    config = ComponentConfig(
        params=params,
        ctx=ports.ctx,
        target=continuity.get("target", "local"),
        name=continuity.get("name", "default"),
    )
    dry_run = not args.write
    try:
        # 写者排他由上面这次正常装配持有：``_build_kernel`` 已经为 continuity 服务抢到
        # 单写锁（活着的服务会让装配本身失败）。这里复用同一进程内的锁持有者，不能再抢
        # 第二次，所以跳过重复抢占——排他性不变，只是不重复声明。
        repository = ContinuityRepository(registry_kv, single_writer=True, allow_ephemeral=True)
    except Exception as exc:
        logger.error(
            "cannot claim the Registry single writer: %s: %s", type(exc).__name__, exc
        )
        pass
        return 2

    migrated = skipped = failed = 0
    try:
        for scope in registry_kv.scopes():
            for head in repository.list(scope, "relations"):
                if "current" in head or not head.get("ref"):
                    skipped += 1
                    continue
                try:
                    relation_id, new_head, history, missing = _migrate_head(memory_kv, head)
                    if not relation_id:
                        raise ValueError("legacy head has no readable relation id")
                    if missing:
                        logger.warning(
                            "history incomplete for relation=%s: %s",
                            relation_id,
                            "; ".join(missing),
                        )
                        failed += 1
                        if _blocks_head_write(missing, args.allow_incomplete_history):
                            # 保留旧 head：覆盖后它会因为已有 `current` 而被永久跳过，
                            # 历史数据恢复后也补不回来。留着重跑即可续迁。
                            skipped += 1
                            continue
                    if dry_run:
                        logger.info(
                            "[dry-run] %s relation=%s revision=%s history=%s",
                            scope,
                            relation_id,
                            new_head["revision"],
                            len(history),
                        )
                        migrated += 1
                        continue
                    for revision, payload in history:
                        repository.save(
                            scope, "relations", f"{relation_id}/rev/{revision}", payload
                        )
                    repository.save(scope, "relations", relation_id, new_head)
                    migrated += 1
                except Exception as exc:
                    logger.error(
                        "migration failed for scope=%s: %s: %s", scope, type(exc).__name__, exc
                    )
                    failed += 1
        # 投影不能只在「本次迁移了头部」时跑：上一次头部成功、投影失败的关系会停在
        # projecting，若这里跳过就永远补不回来。
        if not dry_run:
            pending = _pending_projection(repository)
            if migrated or pending:
                failures = _reproject(config, params, repository)
                # 投影失败必须影响退出码：否则「迁移成功」的假象会把未投影的关系留在库里。
                failed += len(failures)
    finally:
        repository.close()
        # kernel 在装配时抢到了 continuity 单写锁；只关掉本地 repository 不会释放它。
        ports.close()

    mode = "dry-run" if dry_run else "applied"
    if dry_run:
        logger.info("report-only run; pass --write to apply")
    logger.info(
        "mode=%s migrated=%s skipped=%s failed=%s", mode, migrated, skipped, failed
    )
    return 1 if failed else 0


def _blocks_head_write(missing: list[str], allow_incomplete_history: bool) -> bool:
    """Keep the legacy head while history is incomplete so a later run can backfill it."""
    return bool(missing) and not allow_incomplete_history


def _pending_projection(repository: ContinuityRepository) -> list[tuple]:
    """Heads whose projection never completed (status != committed)."""
    pending = []
    for scope in repository.kv.scopes():
        for head in repository.list(scope, "relations"):
            if "current" in head and head.get("status") != "committed":
                pending.append((scope, head))
    return pending


def _reproject(
    config: ComponentConfig,
    params: dict,
    repository: ContinuityRepository,
    *,
    projector=None,
) -> list[str]:
    """Rebuild graph edges from the migrated heads.

    Returns the failed ``relation_id`` list; a failure leaves ``status=projecting`` so the
    next run retries it (see ``_pending_projection``).
    """
    failures: list[str] = []
    if projector is None:
        graph_name = params.get("graph_store", "default")
        if params.get("relation_index_builder"):
            projector = IndexBuilderProducer.dep(config, "relation_index_builder")
        else:
            projector = IndexBuilderProducer.build(
                "evidence_graph", {"graph_store": graph_name}, config.ctx
            )
    for scope in repository.kv.scopes():
        for head in repository.list(scope, "relations"):
            if "current" not in head:
                continue
            relation = RelationRecord.parse(head["current"])
            synthetic = MemoryUnit(
                id=relation.relation_id,
                scope=scope,
                segments=[Segment(content=canonical(asdict(relation)))],
            )
            try:
                projector.build([synthetic], mode=IndexWriteMode.RETRIEVAL_ONLY)
                head["status"] = "committed"
            except Exception as exc:
                head["status"] = "projecting"
                failures.append(f"{relation.relation_id}: {type(exc).__name__}: {exc}")
                logger.error(
                    "projection failed for relation=%s: %s: %s",
                    relation.relation_id,
                    type(exc).__name__,
                    exc,
                )
            repository.save(scope, "relations", relation.relation_id, head)
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
