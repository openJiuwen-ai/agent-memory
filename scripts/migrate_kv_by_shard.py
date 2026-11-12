#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""逻辑分片（F09）运维工具：首次分区、重新分区、对账、索引重建与分片退役。

**在线运维（不需要维护窗口）**：

- ``--reconcile``（不带 ``--include-cleaning``）：清理超期 ``pending`` 与无主数据。
  按协议这两类清理在线安全，应由运维按周期调用——崩溃遗留的 ``pending`` 只能靠它
  解除同 id 的插入阻塞；
- ``--dry-run`` / ``--print-ddl`` / ``--retire-shard <shard> --verify``：只读。

**维护窗口内执行**（运维手册 deploy/sharding/README.md）：

1. 在 API 入口停止接受记忆读写；
2. 等待在途请求与后台任务排空；
3. 设 ``AGENT_MEMORY_MAINTENANCE_WINDOW=1`` 后执行：首次分区、``--repartition``、
   ``--rebuild-indexes``、``--relocate-shard``、退役 ``--begin`` / ``--finish``、
   以及 ``--reconcile --include-cleaning``（回收遗留 ``cleaning``）；
4. 原子切换到分片配置；
5. 重新开放读写。

工具只做数据搬运与校验，不修改配置文件、不删除物理表。分片退役的最后一步只删除
拓扑登记，物理表 / 索引由运维另行归档。

用法示例::

    scripts/migrate_kv_by_shard.py --config prod.yaml --dry-run
    scripts/migrate_kv_by_shard.py --config prod.yaml --reconcile      # 在线
    scripts/migrate_kv_by_shard.py --config prod.yaml --reconcile --include-cleaning  # 维护窗口
    AGENT_MEMORY_MAINTENANCE_WINDOW=1 scripts/migrate_kv_by_shard.py --config prod.yaml
    scripts/migrate_kv_by_shard.py --config prod.yaml --repartition --dry-run
    AGENT_MEMORY_MAINTENANCE_WINDOW=1 ... --repartition --kv-only
    AGENT_MEMORY_MAINTENANCE_WINDOW=1 ... --rebuild-indexes
    scripts/migrate_kv_by_shard.py --config prod.yaml --retire-shard episodic --verify
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from jiuwen_memory.common.bootstrap import register_plugins  # noqa: E402
from jiuwen_memory.common.errors import AgentMemoryError  # noqa: E402
from jiuwen_memory.common.log import get_logger  # noqa: E402
from jiuwen_memory.common.security.bootstrap import register_security  # noqa: E402
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX  # noqa: E402
from jiuwen_memory.common.type_def.memory_codec import loads  # noqa: E402
from jiuwen_memory.config import Config  # noqa: E402
from jiuwen_memory.construction.bootstrap import register_constructors  # noqa: E402
from jiuwen_memory.storage.bootstrap import register_backends  # noqa: E402
from jiuwen_memory.storage.kv import KVStore  # noqa: E402
from jiuwen_memory.storage.shard_routing import (  # noqa: E402
    ShardResolver,
    ShardResolverProducer,
)
from jiuwen_memory.storage.store_manager import StoreManagerProducer  # noqa: E402

_MAINTENANCE_ENV = "AGENT_MEMORY_MAINTENANCE_WINDOW"

logger = get_logger(__name__)


class ToolError(Exception):
    """运维工具的输入 / 环境错误：由 ``main`` 统一转成退出码，不调用 ``SystemExit``。"""


@dataclass(frozen=True)
class Move:
    scope_org: str
    scope_space: str
    scope_user: str
    scope_agent: str
    scope_session: str
    unit_id: str
    source: str
    target: str


def _require_maintenance_window(operation: str) -> None:
    if os.environ.get(_MAINTENANCE_ENV) != "1":
        raise ToolError(
            f"{operation} writes data and requires a maintenance window; "
            f"drain all in-flight requests and set {_MAINTENANCE_ENV}=1"
        )


def _register_runtime() -> None:
    register_plugins()
    register_backends()
    register_constructors()
    register_security()


def _require_shard_admin(port, port_name: str) -> None:
    """管理命令只作用于声明了 ``SHARD_PLACEMENT`` 的分片 KV 端口（S09 #6）。

    不按方法名探测：配错端口时给出明确原因，而不是在调用时抛 ``AttributeError``。
    """
    from jiuwen_memory.storage.capability import StoreCapability, capabilities_of

    if StoreCapability.SHARD_PLACEMENT not in capabilities_of(port):
        raise ToolError(
            f"kv port {port_name!r} does not declare SHARD_PLACEMENT; point --target-kv "
            "at the shard_routed KV port"
        )


def _load(config_path: str):
    _register_runtime()
    from jiuwen_memory.common.factory.factory import Factory
    from jiuwen_memory.config.defaults import default_context

    user = Config.from_yaml(config_path).context(
        known_top_names=Factory.known_top_names()
    )
    ctx = default_context().merged(user)
    name = str(ctx.globals.get("store_manager", "default"))
    manager = StoreManagerProducer.build_named(name, ctx)
    resolver = ShardResolverProducer.build_named("default", ctx)
    return ctx, manager, resolver


def _scopes_with_memory(kv: KVStore):
    for scope in kv.scopes():
        if kv.scan(scope, MEMORY_KEY_PREFIX):
            yield scope


def _units_of(kv: KVStore, scope):
    for key, raw in kv.scan(scope, MEMORY_KEY_PREFIX):
        unit = loads(raw)
        if unit is not None:
            yield unit, key, raw


def plan_partition(source: KVStore, resolver: ShardResolver) -> tuple[list[dict], Counter]:
    """纯规划：读源真源，按规则算目标分片；不写任何数据。"""
    plans: list[dict] = []
    counts: Counter = Counter()
    for scope in _scopes_with_memory(source):
        for unit, key, raw in _units_of(source, scope):
            target = resolver.resolve(unit)
            plans.append(
                {"scope": scope, "key": key, "raw": raw, "shard": target}
            )
            counts[target] += 1
    return plans, counts


def apply_partition(source_kv, target_kv, plans: list[dict]) -> tuple[int, list[str]]:
    """把计划写入目标分片；**保留源记录的剩余 TTL**。

    TTL 在写入时按源记录重新读取，而不是在计划阶段快照：计划到执行之间可能隔了
    一段时间，用旧值会把到期时间整体后移。源记录在此期间被删除、或后端无法给出
    剩余 TTL 时**拒绝迁移该条**并计入失败——按 0 写入会让一条会过期的记忆变成
    永久记录，这是数据语义的静默变更。
    """
    written = 0
    failures: list[str] = []
    for plan in plans:
        scope, key, raw = plan["scope"], plan["key"], plan["raw"]
        try:
            ttl = source_kv.remaining_ttl(scope, key)
            if ttl is None:
                failures.append(
                    f"{key}: TTLUnavailable (source row missing or already expired, or "
                    "backend cannot report remaining TTL; refusing to migrate a "
                    "would-be permanent copy)"
                )
                continue
            target_kv.insert(scope, key, raw, ttl=ttl)
            written += 1
        except AgentMemoryError as exc:
            failures.append(f"{key}: {type(exc).__name__}")
    return written, failures


def plan_repartition(target_kv, resolver: ShardResolver) -> list[Move]:
    """扫描分片真源，按**当前**内容重新计算归属，返回需要移动的记忆。"""
    moves: list[Move] = []
    for scope in _scopes_with_memory(target_kv):
        for unit, key, _raw in _units_of(target_kv, scope):
            location = target_kv.locate(scope, [unit.id])
            current = location.found.get(unit.id)
            desired = resolver.resolve(unit)
            if current is not None and current != desired:
                moves.append(
                    Move(
                        scope.org,
                        scope.space,
                        scope.user,
                        scope.agent,
                        scope.session,
                        unit.id,
                        current,
                        desired,
                    )
                )
    return moves


def apply_moves(target_kv, moves: list[Move]) -> tuple[int, list[str]]:
    from jiuwen_memory.common.type_def import Scope

    moved = 0
    failures: list[str] = []
    for move in moves:
        scope = Scope(
            org=move.scope_org,
            space=move.scope_space,
            user=move.scope_user,
            agent=move.scope_agent,
            session=move.scope_session,
        )
        try:
            target_kv.repartition_unit(scope, move.unit_id, move.target)
            moved += 1
        except AgentMemoryError as exc:
            failures.append(f"{move.unit_id}: {type(exc).__name__}")
    return moved, failures


def print_ddl(resolver: ShardResolver, out=None) -> None:
    out = out or sys.stdout
    template = (Path(__file__).resolve().parent / "pg_schema_shard_template.sql").read_text()
    for shard in resolver.shards:
        out.write(template.replace("{{shard}}", shard))
        out.write("\n")


def rebuild_indexes(
    ctx,
    manager,
    *,
    builder_name: str = "default",
    batch_size: int = 100,
    dry_run: bool = False,
) -> int:
    """按当前真源重建派生索引（维护窗口内）。

    重新分区 / 物理迁移只搬 KV，派生索引仍指向旧分片；在重建完成前按 ``shards``
    召回会漏掉这些记忆。本入口先 ``remove(SOFT)`` 清掉各分片上的旧索引记录
    （向量/全文按 id 扇出删除、实体解除关联），再 ``build(RETRIEVAL_ONLY)`` 按
    **当前归属**重新建索引；正排本体不动，可反复执行。

    ``builder_name`` 对应 ``constructor`` 命名空间下的具名 IndexBuilder（默认
    ``default``，必须写正排 KV），且**必须与生产写入使用同一个构建器**：重建只会产出
    该构建器本身负责的投影（例如 ``unified`` 不写独立向量 / 全文索引，重建它也只会
    重建实体关联）。``dry_run`` 只统计待重建记忆数，不写任何索引。
    """
    from jiuwen_memory.construction.index_builder import IndexBuilderProducer
    from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

    builder = None if dry_run else IndexBuilderProducer.build_named(builder_name, ctx)
    kv = manager.kv()
    total = 0
    for scope in kv.scopes():
        entries = kv.scan(scope, MEMORY_KEY_PREFIX)
        # KVStore.scan 返回列表，原始字节无法再流式；这里只把反序列化后的 MemoryUnit
        # 限制在一个批次内，避免整个 scope 的对象同时驻留内存。
        for offset in range(0, len(entries), batch_size):
            batch = []
            for _key, raw in entries[offset:offset + batch_size]:
                unit = loads(raw)
                if unit is not None:
                    batch.append(unit)
            total += len(batch)
            if builder is None or not batch:
                continue
            builder.remove(batch, mode=IndexRemoveMode.SOFT)
            builder.build(batch, mode=IndexWriteMode.RETRIEVAL_ONLY)
    return total


def _relocate(args, manager, target_kv) -> int:
    _require_maintenance_window("--relocate-shard")
    shard = args.relocate_shard
    if not args.destination_kv:
        raise ToolError("--relocate-shard requires --destination-kv")
    source = target_kv.shard_store(shard)
    destination = manager.kv(args.destination_kv)
    copied = 0
    for scope in source.scopes():
        for key, raw in source.scan(scope):
            copied += 1
            if args.dry_run:
                continue
            ttl = source.remaining_ttl(scope, key)
            if ttl is None:
                raise ToolError(
                    f"relocate refused at {key!r}: source row missing or already expired, "
                    "or backend cannot report remaining TTL; copying with ttl=0 would "
                    "make it permanent"
                )
            try:
                destination.insert(scope, key, raw, ttl=ttl)
            except AgentMemoryError as exc:
                raise ToolError(
                    f"relocate failed at {key!r}: {type(exc).__name__}"
                ) from exc
            if destination.get(scope, key) != raw:
                raise ToolError(f"relocate verification failed at {key!r}")
    logger.info(f"relocate-shard {shard}: {copied} record(s) copied")
    if args.dry_run:
        return 0
    ports: dict[str, str] = {}
    for item in args.descriptor:
        if "=" not in item:
            raise ToolError(f"invalid --descriptor {item!r}; expected PORT=IDENTITY")
        port, identity = item.split("=", 1)
        ports[port] = identity
    if not ports:
        raise ToolError("--relocate-shard requires at least one --descriptor")
    target_kv.update_topology(shard, ports)
    logger.info(f"relocate-shard {shard}: topology updated; switch config and restart")
    return 0


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="部署配置文件（YAML）")
    parser.add_argument("--source-kv", default="legacy", help="首次分区的源 KV 端口名")
    parser.add_argument("--target-kv", default="default", help="分片 KV 端口名")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写数据")
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="在线对账：清理超期 pending / 无主数据（不需维护窗口）",
    )
    parser.add_argument(
        "--include-cleaning",
        action="store_true",
        help="对账时同时清理遗留 cleaning（仅维护窗口、已排空时可用）",
    )
    parser.add_argument("--repartition", action="store_true", help="按当前内容重新分区")
    parser.add_argument(
        "--relocate-pending",
        action="store_true",
        help="执行改路由字段后的待搬迁记录（在线；F09 D16）",
    )
    parser.add_argument(
        "--recover-stale",
        action="store_true",
        help="维护窗口内恢复遗留的 moving 搬迁（配合 --relocate-pending）",
    )
    parser.add_argument("--limit", type=int, default=100, help="单次搬迁 / 重建的记忆数上限")
    parser.add_argument(
        "--min-age",
        type=float,
        default=None,
        help="只处理标记已超过该秒数的待搬迁记录（缺省取 relocate_min_age，默认 30）",
    )
    parser.add_argument(
        "--kv-only",
        action="store_true",
        help="确认重新分区只搬 KV、派生索引需另行重建",
    )
    parser.add_argument("--retire-shard", default="", help="退役指定分片")
    parser.add_argument("--begin", action="store_true", help="退役第 1 步：标记 retiring")
    parser.add_argument("--verify", action="store_true", help="退役第 3 步：校验无残留")
    parser.add_argument("--finish", action="store_true", help="退役第 4 步：删除拓扑登记")
    parser.add_argument("--print-ddl", action="store_true", help="按配置分片打印 KV DDL 模板")
    parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="按当前真源重建向量 / 全文 / 实体索引（需维护窗口；可用 --dry-run 预览）",
    )
    parser.add_argument(
        "--index-builder",
        default="default",
        help="重建时使用的 constructor 具名实例（默认 default，必须写正排 KV）",
    )
    parser.add_argument("--relocate-shard", default="", help="把某分片数据搬到新资源")
    parser.add_argument(
        "--destination-kv",
        default="",
        help="搬迁目标 KV 端口名（--relocate-shard 必填）",
    )
    parser.add_argument(
        "--descriptor",
        action="append",
        default=[],
        metavar="PORT=IDENTITY",
        help="搬迁后的物理标识，可重复（如 kv=host:5432/db/table）",
    )
    args = parser.parse_args(argv)

    _ctx, manager, resolver = _load(args.config)
    target_kv = manager.kv(args.target_kv)

    if args.print_ddl:
        print_ddl(resolver)
        return 0

    _require_shard_admin(target_kv, args.target_kv)

    if args.relocate_shard:
        return _relocate(args, manager, target_kv)

    if args.retire_shard:
        shard = args.retire_shard
        if args.begin:
            _require_maintenance_window("retire-shard --begin")
            target_kv.retire_shard(shard)
            logger.info(f"shard {shard!r} marked retiring; remove it from rules before restarting")
            return 0
        if args.verify:
            reasons = target_kv.verify_retirement(shard)
            if reasons:
                logger.info("cannot retire:")
                for reason in reasons:
                    logger.info(f"  - {reason}")
                return 1
            logger.info(f"shard {shard!r} has no remaining placements or data")
            return 0
        if args.finish:
            _require_maintenance_window("retire-shard --finish")
            target_kv.finish_retirement(shard)
            logger.info(f"shard {shard!r} topology entry removed")
            return 0
        parser.error("--retire-shard requires one of --begin / --verify / --finish")

    if args.rebuild_indexes:
        if not args.dry_run:
            _require_maintenance_window("--rebuild-indexes")
        rebuilt = rebuild_indexes(
            _ctx,
            manager,
            builder_name=args.index_builder,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            logger.info(f"rebuild-indexes: {rebuilt} memory unit(s) would be rebuilt")
        else:
            logger.info(
                f"rebuild-indexes: rebuilt {rebuilt} memory unit(s) "
                f"with index_builder {args.index_builder!r}"
            )
        return 0

    if args.reconcile:
        # 清理 pending / 无主数据是在线安全操作；只有回收遗留 cleaning 才要求维护窗口。
        if args.include_cleaning:
            _require_maintenance_window("--reconcile --include-cleaning")
        report = target_kv.reconcile(include_cleaning=args.include_cleaning)
        logger.info(
            "reconcile: expired_pending={expired_pending} orphan_data={orphan_data} "
            "leftover_cleaning={leftover_cleaning} skipped={skipped}".format(
                **report.__dict__
            )
        )
        return 0

    if args.relocate_pending:
        if args.recover_stale:
            _require_maintenance_window("--relocate-pending --recover-stale")
        report = target_kv.relocate_pending(
            limit=args.limit,
            min_age=args.min_age,
            recover_stale=args.recover_stale,
        )
        logger.info(f"relocate-pending: {report}")
        return 0

    if args.repartition:
        if not args.kv_only and not args.dry_run:
            raise ToolError(
                "--repartition moves KV placement/data only and does NOT rebuild vector/"
                "fulltext/entity/graph indexes. Run `--rebuild-indexes` afterwards, "
                "or re-run with --kv-only to acknowledge this limitation."
            )
        moves = plan_repartition(target_kv, resolver)
        logger.info(f"repartition: {len(moves)} memory unit(s) would move")
        for move in moves:
            logger.info(f"  {move.unit_id}: {move.source} -> {move.target}")
        if args.dry_run:
            return 0
        _require_maintenance_window("--repartition")
        moved, failures = apply_moves(target_kv, moves)
        logger.info(f"repartition: moved={moved} failed={len(failures)}")
        for failure in failures:
            logger.info(f"  ! {failure}")
        return 0 if not failures else 1

    source_kv = manager.kv(args.source_kv)
    plans, counts = plan_partition(source_kv, resolver)
    logger.info(f"partition: {len(plans)} memory unit(s)")
    for shard, count in sorted(counts.items()):
        logger.info(f"  {shard}: {count}")
    if args.dry_run:
        return 0
    _require_maintenance_window("partition")
    written, failures = apply_partition(source_kv, target_kv, plans)
    logger.info(f"partition: written={written} failed={len(failures)}")
    targets = Counter(plan["shard"] for plan in plans)
    logger.info(f"partition: expected shards {dict(sorted(targets.items()))}")
    for failure in failures:
        logger.info(f"  ! {failure}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：把输入 / 环境错误转成退出码，只有进程入口使用 ``SystemExit``。"""
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True
    )
    try:
        return _run(argv)
    except ToolError as exc:
        logger.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
