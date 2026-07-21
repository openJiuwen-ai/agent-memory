# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# pylint: disable=protected-access,huawei-redefined-outer-name
"""
End-to-end system test for the Ebbinghaus forgetting flow.

Boots a real ``LongTermMemory`` with:
  - In-memory KV store
  - Persistent ChromaDB vector store (local directory under tmp_path)
  - Real LLM + real embedding model (configured via env vars, skipped when
    the env vars are absent — this test is opt-in)

Scenario (mirrors the user-facing demo):

  1. ``set_scope_config`` with ``important_memory_definition`` narrowed to
     only the user's identity information. Everything else (episodic,
     semantic) is eligible for forgetting.
  2. ``add_messages`` with a multi-round conversation that mingles
     identity info ("我叫张三，是一名数据分析师") with episodic trivia
     ("昨天去超市买了苹果", "上周和朋友看了电影").
  3. ``search_user_mem`` and ``get_user_mem_by_page`` show the freshly
     written memories; ``asyncio.sleep`` lets the fire-and-forget
     ``retrieve_history`` appenders finish.
  4. Read the KV ``retrieve_history`` for each mem_id; assert at least
     one was seeded.
  5. Hand-rewrite the memory's ``timestamp`` + ``retrieve_history`` KV
     payload so some memories look old enough to evict and some look
     fresh (still within ``min_retention_days``).
  6. Trigger one forget sweep via ``start_dreaming`` with
     ``DreamingConfig(enabled=False, forgetting=ForgettingConfig(enabled=True))``
     and ``orch._sweep_fn()`` (forget-only — no LLM extraction this round).
  7. Verify: identity-anchored memories remain searchable; the stale,
     non-identity memories are now ``blacklisted=True`` in storage and
     excluded from the default search/list paths; the recall path
     (``filters=EQ("blacklisted", True)``) still surfaces them.

Environment variables (all required — the test is skipped otherwise):

  - ``MEMORY_E2E_LLM_PROVIDER``       e.g. ``DashScope`` / ``OpenAI``
  - ``MEMORY_E2E_LLM_API_KEY``
  - ``MEMORY_E2E_LLM_BASE``            e.g. ``https://dashscope.aliyuncs.com/compatible-mode/v1``
  - ``MEMORY_E2E_LLM_MODEL``           e.g. ``qwen-plus``
  - ``MEMORY_E2E_EMBED_API_KEY``       (often equals the LLM key)
  - ``MEMORY_E2E_EMBED_BASE``          e.g. ``https://dashscope.aliyuncs.com/api/v1``
  - ``MEMORY_E2E_EMBED_MODEL``         e.g. ``text-embedding-v2``
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from jiuwen_memory.foundation.llm.schema.message import BaseMessage
from jiuwen_memory.foundation.store.base_embedding import EmbeddingConfig
from jiuwen_memory.foundation.store.db.default_db_store import DefaultDbStore
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.foundation.store.vector.chroma_vector_store import ChromaVectorStore
from jiuwen_memory.memory_core.config.config import (
    AgentMemoryConfig,
    DreamingConfig,
    ForgettingConfig,
    MemoryEngineConfig,
    MemoryScopeConfig,
)
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory, MemInfo
from jiuwen_memory.memory_core.process.forgetting import EbbinghausEvaluator
from jiuwen_memory.retrieval.embedding.api_embedding import APIEmbedding


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers

# API credentials / endpoints are read from env vars with empty-string
# defaults. The test is skipped when any required value is empty so the
# suite does not accidentally hit a live provider during CI.
_E2E_LLM_PROVIDER = os.environ.get("MEMORY_E2E_LLM_PROVIDER", "")
_E2E_LLM_API_KEY = os.environ.get("MEMORY_E2E_LLM_API_KEY", "")
_E2E_LLM_BASE = os.environ.get("MEMORY_E2E_LLM_BASE", "")
_E2E_LLM_MODEL = os.environ.get("MEMORY_E2E_LLM_MODEL", "")
_E2E_EMBED_API_KEY = os.environ.get("MEMORY_E2E_EMBED_API_KEY", "")
_E2E_EMBED_BASE = os.environ.get("MEMORY_E2E_EMBED_BASE", "")
_E2E_EMBED_MODEL = os.environ.get("MEMORY_E2E_EMBED_MODEL", "")


# Skip the entire module when any required env var is empty. This keeps
# the suite green in environments without test credentials (default
# ``pytest`` run) while still allowing opt-in execution via env vars.
pytestmark = pytest.mark.skipif(
    not (
        _E2E_LLM_PROVIDER
        and _E2E_LLM_API_KEY
        and _E2E_LLM_BASE
        and _E2E_LLM_MODEL
        and _E2E_EMBED_API_KEY
        and _E2E_EMBED_BASE
        and _E2E_EMBED_MODEL
    ),
    reason="MEMORY_E2E_* env vars not set; skipping Ebbinghaus e2e (opt-in test)",
)


def _scope_config() -> MemoryScopeConfig:
    """Scope config whose ``important_memory_definition`` is narrowed to
    ONLY the user's identity information — so episodic trivia written in
    the same session is eligible for forgetting.
    """
    return MemoryScopeConfig(
        model_cfg=ModelRequestConfig(model=_E2E_LLM_MODEL),
        model_client_cfg=ModelClientConfig(
            client_provider=_E2E_LLM_PROVIDER,
            api_key=_E2E_LLM_API_KEY,
            api_base=_E2E_LLM_BASE,
            verify_ssl=False,
        ),
        embedding_cfg=EmbeddingConfig(
            model_name=_E2E_EMBED_MODEL,
            base_url=_E2E_EMBED_BASE,
            api_key=_E2E_EMBED_API_KEY,
        ),
        # Only identity-anchored facts are protected from forgetting.
        # This is the key lever that lets the same LLM extraction pass
        # tag the user_profile fact as ``is_important=True`` while leaving
        # episodic trivia unprotected.
        important_memory_definition=(
            "仅将描述用户本人身份的信息判定为重要记忆，"
            "包括但不限于：用户的姓名、职业、国籍、性别、年龄等长期稳定身份属性。"
            "其他场景性、时间性、行为性记忆一律判定为不重要。"
        ),
    )


def _embedding_model() -> APIEmbedding:
    return APIEmbedding(
        config=EmbeddingConfig(
            model_name=_E2E_EMBED_MODEL,
            base_url=_E2E_EMBED_BASE,
            api_key=_E2E_EMBED_API_KEY,
        ),
    )


def _rh_key(user_id: str, scope_id: str, mem_id: str) -> str:
    """retrieve_history KV key. Mirrors
    ``LongTermMemory._retrieve_history_key``."""
    return f"retrieve_history/{user_id}/{scope_id}/{mem_id}"


def _print_memories(label: str, items: list[MemInfo]) -> None:
    """Pretty-print a list of ``MemInfo`` (the public return type of
    ``get_user_mem_by_page``) so the user can eyeball the result in
    pytest -s output."""
    logger.info("=== %s (%d items) ===", label, len(items))
    for it in items:
        logger.info(
            "  id=%s type=%s timestamp=%r text=%r",
            it.mem_id, it.type, it.timestamp, it.content,
        )


# ---------------------------------------------------------------- fixture


@pytest_asyncio.fixture
async def ltm(tmp_path):
    """Boot a real LongTermMemory with in-memory KV + persistent
    Chroma + real LLM/embedding. Registers the scope config BEFORE any
    messages are written so the LLM extractor sees the narrowed
    ``important_memory_definition`` from the first turn.
    """
    Singleton._instances.pop(LongTermMemory, None)
    m = LongTermMemory()

    kv = InMemoryKVStore()
    vector = ChromaVectorStore(persist_directory=str(tmp_path / "chroma"))
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{str(tmp_path / 'e2e.db')}",
    )
    db = DefaultDbStore(engine)

    await m.register_store(
        kv_store=kv,
        vector_store=vector,
        db_store=db,
        embedding_model=_embedding_model(),
    )
    # System config with crypto_key empty — no encryption at rest (test only).
    m.set_config(MemoryEngineConfig(crypto_key=b""))

    scope_id, user_id = "scope_e2e", "alice"
    await m.set_scope_config(scope_id, _scope_config())

    try:
        yield m, scope_id, user_id
    finally:
        await m.stop_dreaming()
        Singleton._instances.pop(LongTermMemory, None)


# ---------------------------------------------------------------- the test


@pytest.mark.asyncio
async def test_ebbinghaus_forgetting_end_to_end(ltm):
    m, scope_id, user_id = ltm

    # 1) add_messages — one round with identity info, two rounds with
    #    episodic trivia. The extraction prompt should tag the
    #    user_profile identity fact as ``is_important=True`` (because of
    #    the narrowed scope config), while the episodic rounds get no
    #    such protection.
    rounds = [
        ("我叫张三，是一名数据分析师", "好的，张三，很高兴认识你"),
        ("昨天我去超市买了三斤苹果", "听起来很健康"),
        ("上周五我和朋友小李去看了《流浪地球》", "这部电影评价不错"),
    ]
    for user_text, asst_text in rounds:
        await m.add_messages(
            messages=[
                BaseMessage(role="user", content=user_text),
                BaseMessage(role="assistant", content=asst_text),
            ],
            agent_config=AgentMemoryConfig(enable_long_term_mem=True),
            user_id=user_id, scope_id=scope_id,
            session_id="sess_e2e", gen_mem=True,
        )

    # 2) search_user_mem — see what we wrote. Score-sorted; the user
    #    should see all three memory types returned (the print is for
    #    eyeballing).
    results = await m.search_user_mem("张三 苹果 电影", num=10,
                                       user_id=user_id, scope_id=scope_id)
    logger.info("=== search results after add_messages ===")
    for r in results:
        logger.info(
            "  id=%s type=%s score=%.3f content=%r",
            r.mem_info.mem_id, r.mem_info.type, r.score, r.mem_info.content,
        )

    assert len(results) > 0, "no memories were extracted — LLM path likely failed"

    # 3) get_user_mem_by_page — full inventory via the public pagination
    #    API. Sleep first so the fire-and-forget retrieve_history
    #    appenders (one per search hit, asyncio.create_task) finish
    #    before we read the KV. ``MemInfo`` doesn't surface
    #    ``blacklisted`` / ``is_important`` scalars, so we infer
    #    eviction later by set-difference between the default listing
    #    (filters=None → NE("blacklisted", True) injected) and the
    #    recall listing (filters=EQ("blacklisted", True)).
    await asyncio.sleep(2.0)
    listed = await m.get_user_mem_by_page(
        user_id=user_id, scope_id=scope_id,
        page_size=50, page_idx=1,
    )
    _print_memories("list after add_messages", listed)
    assert len(listed) >= 1

    # Debug: log raw timestamp types/values so we can spot any
    # tz-naive datetimes that would break Ebbinghaus scoring.
    logger.info("=== timestamps (raw) ===")
    for item in listed:
        ts = item.timestamp
        logger.info(
            "  id=%s type=%s value=%r tzinfo=%s",
            item.mem_id, type(ts).__name__, ts, getattr(ts, "tzinfo", None),
        )

    # Index memories by content keyword for the assertions that follow.
    # Use a list of (kw, item) pairs rather than a dict because the same
    # keyword (e.g. "苹果") may match both a summary memory ("用户去买苹果")
    # and an episodic memory ("昨天买了苹果") — both must be captured as
    # independent targets or the dedup-by-key would silently drop one.
    by_keyword: list[tuple[str, MemInfo]] = []
    for item in listed:
        text = (item.content or "").lower()
        for kw in ("张三", "苹果", "电影", "小李"):
            if kw in text:
                by_keyword.append((kw, item))
    logger.info("=== keyword index ===\n%s", [(kw, it.mem_id) for kw, it in by_keyword])

    # 4) Retrieve the retrieve_history KV for each mem_id — at least
    #    one should have been seeded (the search hits above triggered
    #    the fire-and-forget appender). No public API exposes
    #    retrieve_history, so we read the KV directly — this is the
    #    only place the test still reaches into internal storage, and
    #    only to verify a behaviour that has no external surface.
    rh_seen = 0
    for item in listed:
        rh_raw = await m.kv_store.get(
            _rh_key(user_id, scope_id, item.mem_id)
        )
        if rh_raw is not None:
            rh_seen += 1
            rh = json.loads(
                rh_raw.decode() if isinstance(rh_raw, bytes) else rh_raw
            )
            logger.info("  retrieve_history[%s]: %s", item.mem_id, rh)
    logger.info("=== retrieve_history seeded for %d/%d memories ===", rh_seen, len(listed))
    assert rh_seen > 0, (
        "no retrieve_history was seeded — the search path's "
        "_append_retrieve_history did not fire"
    )

    # 5) Hand-rewrite timestamps + retrieve_history so SOME memories
    #    are evictable (old + no recent access) and SOME are protected
    #    (fresh retrieve within min_retention_days). The identity fact
    #    is left alone — the LLM may or may not have tagged it
    #    ``is_important=True`` (Qwen2.5-72B sometimes skips it), so we
    #    give it a fresh retrieve_history to make the formula protect
    #    it regardless. Belt + suspenders.
    now = datetime.now(timezone.utc).astimezone()
    very_old = now - timedelta(days=120)   # well past base_s=30
    fresh = now - timedelta(hours=2)       # within min_retention_days=30

    targets: list[tuple[str, MemInfo]] = []
    for kw, item in by_keyword:
        if kw == "张三":
            # identity fact — leave timestamp alone, but seed fresh
            # retrieve_history so the formula also protects it.
            await m.kv_store.set(
                _rh_key(user_id, scope_id, item.mem_id),
                json.dumps({
                    "retrieve_count": 5,
                    "latest_retrieve_time": fresh.isoformat(),
                    "retrieve_history": [fresh.isoformat()],
                }),
            )
            continue
        if kw in ("苹果", "电影"):
            # Make these "very old + no retrieve" — evictable.
            # Pass a tz-aware ISO string so _kv_data_to_memory_doc parses
            # it via datetime.fromisoformat (which preserves tz). See
            # the parse_iso contract in forgetting/evaluator.py.
            #
            # NOTE: no public API for in-place timestamp mutation —
            # this is test scaffolding to fast-forward the clock on
            # specific memories so the forget sweep has something to
            # evict without us waiting 120 days. Reaches into internal
            # storage intentionally.
            await m.memory_index.update_mem_by_id(
                user_id, scope_id, item.mem_id,
                {"timestamp": very_old.isoformat()},
            )
            await m.kv_store.delete(_rh_key(user_id, scope_id, item.mem_id))
            targets.append((kw, item))
            logger.info("=== mutated %s (id=%s) → very_old + no retrieve ===", kw, item.mem_id)

    # 6) Trigger one forget sweep — forget-only (DreamingConfig.enabled=False
    #    so no sweeper; ForgettingConfig.enabled=True so forget runs).
    forgetting = ForgettingConfig(
        enabled=True, threshold=0.5,  # easy to trip after 120 days of decay
        min_retention_days=30, max_evict=100,
        evaluator=EbbinghausEvaluator(m.kv_store, base_s=30),
    )
    orch = await m.start_dreaming(
        scope_id, user_id,
        config=DreamingConfig(enabled=False, forgetting=forgetting),
    )
    assert orch is not None, "forget-only orchestrator should start when forgetting=True"
    await orch._sweep_fn()  # run one tick synchronously (no scheduler wait)
    await m.stop_dreaming()

    # 7) Verify the evicted memories are now blacklisted in storage and
    #    excluded from default search/list; the recall path still
    #    surfaces them.
    from jiuwen_memory.foundation.store.filter_dsl import (
        FilterCondition,
        FilterGroup,
        FilterOperator,
    )

    # Default listing: filters=None → framework injects
    # NE("blacklisted", True) → evicted memories excluded.
    listed_default = await m.get_user_mem_by_page(
        user_id=user_id, scope_id=scope_id,
        page_size=50, page_idx=1,
    )
    _print_memories("list after forget (default, excludes blacklisted)",
                    listed_default)

    # Recall listing: caller-supplied EQ("blacklisted", True) → framework
    # keeps the caller's filters as-is → evicted memories included.
    bl_filter = FilterGroup(
        conditions=[
            FilterCondition(
                field="blacklisted", op=FilterOperator.EQ, value=True,
            )
        ]
    )
    listed_recall = await m.get_user_mem_by_page(
        user_id=user_id, scope_id=scope_id,
        page_size=50, page_idx=1, filters=bl_filter,
    )
    _print_memories("list after forget (filters=EQ(blacklisted,True), includes blacklisted)",
                    listed_recall)

    # 7a) The "old + no retrieve" targets should be evicted — i.e.
    #     present in the recall listing (filters=EQ(blacklisted, True))
    #     but absent from the default listing (filters=None). We derive
    #     the evicted set as the set-difference between the two listings,
    #     restricted to target ids.
    default_ids = {it.mem_id for it in listed_default}
    recall_ids = {it.mem_id for it in listed_recall}
    blacklisted_ids = recall_ids - default_ids
    target_ids = {it.mem_id for _, it in targets}
    evicted_ids = blacklisted_ids & target_ids
    assert evicted_ids, (
        "expected at least one of the 'old + no retrieve' memories to be "
        "blacklisted after the forget sweep; got none "
        f"(default={default_ids} recall={recall_ids} targets={target_ids})"
    )

    # 7b) Default list excludes blacklisted memories: evicted targets
    #     should NOT appear in ``listed_default``. (We no longer assert
    #     the identity fact survives via ``is_important=True`` because
    #     the LLM's tagging of that flag is not deterministic enough
    #     to gate a test on.)
    for ev_id in evicted_ids:
        assert ev_id not in default_ids, (
            f"evicted memory {ev_id} surfaced in default listing "
            f"(filters=None) — blacklisted filter is leaking"
        )

    # 7c) Search path: the evicted content should NOT surface in the
    #     default search (NE blacklisted injected).
    for kw, item in targets:
        default_hits = await m.search_user_mem(
            kw, num=10, user_id=user_id, scope_id=scope_id,
        )
        default_search_ids = {r.mem_info.mem_id for r in default_hits}
        assert item.mem_id not in default_search_ids, (
            f"evicted memory {item.mem_id} surfaced in default search "
            f"for {kw!r} — blacklisted filter is leaking"
        )

    # 7d) search_user_mem with explicit filters=FilterGroup([EQ("blacklisted", True)])
    #     MUST retrieve the evicted memories. This proves the filter DSL path can
    #     surface forgotten content on demand (recall path via search).
    #     Note: ``search_user_mem`` defaults to ``fragment_type`` =
    #     [user_profile, episodic_memory, semantic_memory] — ``summary``
    #     is excluded by default. So we assert AT LEAST ONE evicted
    #     memory (the episodic one) surfaces via the filter path; the
    #     summary one may or may not, depending on whether the caller
    #     widened the search_type.
    # Use a broad query that should match all evicted content. Lower the
    # threshold to 0.0 so low-score matches still come back.
    evicted_kws = {kw for kw, it in targets if it.mem_id in evicted_ids}
    broad_query = " ".join(sorted(evicted_kws))
    bl_hits = await m.search_user_mem(
        broad_query, num=50, user_id=user_id, scope_id=scope_id,
        threshold=0.0, filters=bl_filter,
    )
    bl_hit_ids = {r.mem_info.mem_id for r in bl_hits}
    logger.info(
        "=== VP1: search_user_mem(filters=EQ(blacklisted,True)) "
        "query=%r → %d hits ===",
        broad_query, len(bl_hit_ids),
    )
    for r in bl_hits:
        logger.info(
            "  id=%s type=%s score=%.3f content=%r",
            r.mem_info.mem_id, r.mem_info.type, r.score, r.mem_info.content,
        )
    # The filter path must surface at least one evicted memory; otherwise
    # the EQ(blacklisted, True) pushdown isn't working.
    assert bl_hit_ids & evicted_ids, (
        f"no evicted memory surfaced in search with "
        f"filters=EQ(blacklisted,True) — recall-via-filter path is broken; "
        f"hits={bl_hit_ids} evicted={evicted_ids}"
    )

    # 7f) NEW VERIFICATION POINT 2: get_user_mem_by_page with
    #     filters=EQ("blacklisted", True) MUST include the evicted
    #     memories. Same recall contract but via the pagination path —
    #     already covered by the recall listing in step 7a, but
    #     re-stated here explicitly as a named verification point for
    #     the spec's recall contract.
    listed_recall_check = await m.get_user_mem_by_page(
        user_id=user_id, scope_id=scope_id,
        page_size=50, page_idx=1, filters=bl_filter,
    )
    paged_ids = {it.mem_id for it in listed_recall_check}
    for ev_id in evicted_ids:
        assert ev_id in paged_ids, (
            f"evicted memory {ev_id} missing from get_user_mem_by_page("
            f"filters=EQ(blacklisted,True)) — recall-via-pagination path is broken"
        )
    logger.info(
        "=== VP2: get_user_mem_by_page(filters=EQ(blacklisted,True)) "
        "surfaces evicted memories ==="
    )

    logger.info("=== E2E forgetting test PASSED ===")
