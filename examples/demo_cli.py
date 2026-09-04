# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI 本地调用演示：使用 MemoryAPI 原参数，读取原返回值。

运行：``uv run --no-sync python examples/demo_cli.py``

显式使用 dev 认证（固定 local/developer 身份），仅供本地功能测试。
同一个 InProcessClient 共享内核，最后释放资源。CLI 不经过 legacy handler；
add 返回 MemoryUnit 数组，search 返回含 items 的 SearchResult，delete 返回 ID 数组。
末段保留可选 Source / FS / Fusion / SQLite 组件的独立演示。
"""

from __future__ import annotations

import io
import logging
import os
import sys
from importlib import import_module

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.append(_REPO)

make_client = import_module("jiuwen_memory_entry.cli.client").make_client

SCOPE = {"org": "local", "user": "developer"}
logger = logging.getLogger(__name__)


def hr(title: str) -> None:
    logger.info("\n== %s ==", title)


def main() -> int:
    """Run API-shaped calls against one explicitly authenticated local runtime."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = make_client(None, auth_mode="dev")

    def call(method: str, **payload):
        status, body = client.call(method, payload)
        if not 200 <= status < 300:
            raise RuntimeError(f"{method} failed ({status}): {body}")
        return body

    try:
        hr("healthz")
        logger.info("  %s", client.healthz()[1])

        hr("add — 原返回值是 MemoryUnit 数组")
        added = call("add", content="Alice 喜欢咖啡 coffee，不加糖", scope=SCOPE, tags=["coffee"])
        unit_id = added[0]["id"]
        logger.info("  id=%s segments=%s", unit_id, added[0]["segments"])

        hr("search — 使用 context / top_k，结果读取 items / unit_id")
        result = call("search", query="coffee", context={"scope": SCOPE}, top_k=5)
        logger.info("  %s", result["items"])

        hr("get / update — 使用 unit_id / scope / patch")
        logger.info("  get: %s", call("get", unit_id=unit_id, scope=SCOPE))
        updated = call(
            "update",
            unit_id=unit_id,
            scope=SCOPE,
            patch={"content": "Alice 改喝拿铁 coffee，要燕麦奶", "mode": "overwrite"},
        )
        unit_id = updated["id"]
        logger.info("  update: %s", updated)

        hr("add_async / batch_add_async — 等待原方法完成，不转换为 job")
        logger.info("  add_async: %s", call("add_async", content="async memory", scope=SCOPE))
        logger.info(
            "  batch_add_async: %s",
            call("batch_add_async", items=[{"content": "batch memory"}], scope=SCOPE),
        )

        hr("list / inspect / trace — 保留原列表与对象结构")
        logger.info("  list: %s", call("list", scope=SCOPE))
        logger.info("  inspect: %s", call("inspect", unit_ids=[unit_id], scope=SCOPE))
        logger.info("  trace: %s", call("trace", unit_id=unit_id, scope=SCOPE))

        hr("delete — 使用 selector，返回被删除的 ID 列表")
        deleted = call("delete", selector={"scope": SCOPE, "unit_ids": [unit_id], "mode": "purge"})
        logger.info("  deleted: %s", deleted)
        _aux_components()
        logger.info("demo complete.")
        return 0
    finally:
        client.close()


def _aux_components() -> None:
    """直接演示不在默认装配里的可选/辅助组件。"""
    from datetime import datetime, timezone

    from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
    from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
        WhitespaceTokenizer,
    )
    from jiuwen_memory.common.type_def import FilterClause, FilterOp, Scope
    from jiuwen_memory.ingest.source_impl.text_source import TextSource
    from jiuwen_memory.storage.fs_impl.in_memory_fs_store import InMemoryFSStore
    from jiuwen_memory.storage.fusion_impl.in_memory_fusion_store import InMemoryFusionStore
    from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
    from jiuwen_memory.storage.types import FusionQuery, FusionRecord

    sc = Scope(org="default", user="alice")
    tok = WhitespaceTokenizer()
    emb = HashingEmbedder(tok)

    hr("辅助组件（默认装配外，直接演示）")

    src = TextSource(sc, [("从对话源导入的一条记忆", datetime(2026, 1, 1, tzinfo=timezone.utc))])
    logger.info("  Source.fetch(): %s", [p.data.decode() for p in src.fetch()])

    fs = InMemoryFSStore()
    ref = fs.insert(sc, "photo.png", io.BytesIO(b"\x89PNG fake"))
    logger.info(
        "  FSStore: ref=%s  size=%sB  读回=%r",
        ref,
        fs.stat(sc, ref).size,
        fs.get(sc, ref).read(),
    )

    fz = InMemoryFusionStore(tok)
    fz.insert(
        sc,
        [
            FusionRecord(
                id="a",
                vector=emb.embed(["美式咖啡"])[0],
                text="美式咖啡",
                scalars={"tier": "semantic"},
            ),
            FusionRecord(
                id="b", vector=emb.embed(["绿茶"])[0], text="绿茶", scalars={"tier": "episodic"}
            ),
        ],
    )
    q = FusionQuery(
        vector=emb.embed(["咖啡"])[0],
        text="咖啡",
        vector_weight=0.5,
        scalar_filters=[FilterClause(field="tier", op=FilterOp.EQ, value="semantic")],
    )
    logger.info(
        "  FusionStore.search(咖啡, tier=semantic): %s",
        [(s.id, round(s.score, 3)) for s in fz.search(sc, q)],
    )

    kv = SQLiteKVStore(":memory:")
    kv.insert(sc, "k1", b"bytes-on-disk")
    logger.info(
        "  SQLiteKVStore: get=%s scopes=%s",
        kv.get(sc, "k1"),
        [s.user for s in kv.scopes()],
    )


if __name__ == "__main__":
    sys.exit(main())
