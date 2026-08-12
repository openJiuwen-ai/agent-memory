"""CLI surface 端到端演示——尽量调用全部模块（同进程 dispatch，无需起服务）。

运行：``python3 examples/demo_cli.py``

走 CLI 的 :class:`~bootstrap.cli.client.InProcessClient`（CLI/HTTP 共用的
``handler.dispatch`` 代码路径，少了 socket）。前半段用动词把主链路 + 演进 + 治理 +
管理面都跑一遍；末段直接演示几个不在默认装配里的可选/辅助组件（Source/FS/Fusion/
SQLite）。一个进程内共享内核，状态跨调用持久。无任何外部依赖。

覆盖到的模块（按调用）：
- add     → Ingestor·Normalizer·Classifier·HybridIndexBuilder(Fulltext+Vector+Embedder)·
            KVStore·memory_codec·PermissionManager·AuditLogger·Tokenizer
- search  → QueryParser(+EchoLLM 改写·Embedder)·Keyword/Vector/Graph Recaller·RRFFuser·
            TruncatingDiscloser(+OverlapReranker)
- evolve  → Chunker·Extractor·Abstractor·FeatureExtractor·Associator·GraphStore·Evolver·Scheduler
- job     → Scheduler.status
- inspect/trace → Governor      · audit → Governor+AuditLogger
- admin   → PolicyManager       · grant → PermissionManager
- delete  → LifecycleManager
- 末段     → TextSource·InMemoryFSStore·InMemoryFusionStore·SQLiteKVStore
"""

from __future__ import annotations

import io
import logging
import os
import sys
from importlib import import_module

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLI_DIR = os.path.join(_REPO, "bootstrap", "cli")
if _CLI_DIR not in sys.path:
    sys.path.append(_CLI_DIR)

make_client = import_module("client").make_client

BASE = {"tenant_id": "default", "scope": "alice"}
logger = logging.getLogger(__name__)


def hr(title: str) -> None:
    logger.info("\n\033[1m== %s ==\033[0m", title)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = make_client(None)  # in-process：同一内核，状态跨调用持久

    def call(verb: str, **payload):
        status, body = client.call(verb, {**BASE, **payload})
        if status >= 300:
            logger.info("  [%s] %s: %s", status, body.get("error"), body.get("message"))
        return body

    hr("health")
    logger.info("  %s", client.healthz()[1])

    hr("add — 写入（规约/分类/倒排+向量索引/落 kv/审计）")
    for content, tags in [
        ("Alice 喜欢早上喝美式咖啡，不加糖", ["coffee", "habit"]),
        ("项目 agent-memory 给 AI agent 提供独立记忆子系统", ["project"]),
        ("下周三下午三点和设计团队评审检索链路", ["meeting"]),
    ]:
        it = call("add", content=content, tags=tags)["item"]
        logger.info("  %s  tier=%-9s  %s", it["item_id"][:8], it["tier"], it["content"])

    hr("search '咖啡' — 三路召回(关键词/向量/图)→RRF→精排→披露")
    hits = call("search", query="咖啡", k=5)["hits"]
    for h in hits:
        logger.info("  %.3f  %s  %s", h["score"], h["item_id"][:8], h["content"])
    hit_id = hits[0]["item_id"]

    hr("get / update(SUPERSEDE 记版本链)")
    logger.info("  get : %s", call("get", item_id=hit_id)["item"]["content"])
    new_id = call("update", item_id=hit_id, content="Alice 改喝拿铁，要燕麦奶")["item"]["item_id"]
    logger.info("  update: %s -> %s", hit_id[:8], new_id[:8])

    hr("evolve — 演进闭环（抽取/升华/关联落图/遗忘）+ 任务状态")
    for mode in ("extract", "consolidate", "associate", "forget"):
        r = call("evolve", mode=mode)
        job = call("job", job_id=r["job_id"])
        logger.info("  %-11s job=%s status=%s", mode, r["job_id"][:8], job["status"])
    prof = call("search", query="画像", k=1)["hits"]
    if prof:
        logger.info("  consolidate 画像: %s...", prof[0]["content"][:32])

    hr("inspect / trace — 治理检视与血缘回溯")
    logger.info(
        "  trace 版本链: %s",
        [u["item_id"][:8] for u in call("trace", item_id=new_id)["items"]],
    )
    logger.info(
        "  inspect: %s",
        [u["content"][:16] for u in call("inspect", item_id=new_id)["items"]],
    )

    hr("audit — 审计留痕（按动作过滤）")
    for action in ("add", "evolve", "update"):
        logger.info("  %-7s: %s 条", action, call("audit", action=action)["count"])

    hr("admin — 运行时策略（PolicyManager）")
    logger.info("  all   : %s", call("admin")["policies"])
    logger.info("  set   : %s", call("admin", key="rerank.enabled", value="false"))

    hr("grant — 跨 scope 授权（PermissionManager）")
    logger.info("  %s", call("grant", grantee="bob"))

    hr("delete — 软删除（LifecycleManager 非破坏式流转）")
    call("delete", item_id=hit_id)
    logger.info(
        "  原始项 lifecycle: %s (记录仍在)", call("get", item_id=hit_id)["item"]["lifecycle"]
    )

    _aux_components()

    logger.info("\n\033[1mdemo complete.\033[0m")
    return 0


def _aux_components() -> None:
    """直接演示不在默认装配里的可选/辅助组件（src 路径已由 client.py 接好）。"""
    from datetime import datetime, timezone

    from common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
    from common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
    from common.type_def import FilterClause, FilterOp, Scope
    from ingest.source_impl.text_source import TextSource
    from storage.fs_impl.in_memory_fs_store import InMemoryFSStore
    from storage.fusion_impl.in_memory_fusion_store import InMemoryFusionStore
    from storage.kv_impl.sqlite_kv_store import SQLiteKVStore
    from storage.types import FusionQuery, FusionRecord

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
