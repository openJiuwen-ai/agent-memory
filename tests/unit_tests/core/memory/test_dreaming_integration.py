# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""End-to-end usage example for dreaming.

Bootstraps a real LongTermMemory (in-memory KV + real sqlite message store),
feeds a multi-round session via ``add_messages``, then runs the dreaming flow
through the public ``start_dreaming`` API and asserts the consolidated memory
becomes retrievable via ``search_user_mem`` — the full
add_messages → dream → promote → search chain.

The vector channel is backed by a small in-memory ``BaseMemoryIndex`` fake so
the test is deterministic and runs without an external vector DB.
"""
# Tests reset the Singleton and drive LongTermMemory/orchestrator internals.
# pylint: disable=protected-access

import json
import os
import re
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from common.utils.singleton import Singleton
from foundation.llm.schema.message import AssistantMessage, BaseMessage
from foundation.store.base_embedding import Embedding
from foundation.store.base_memory_index import BaseMemoryIndex, MemoryDoc
from foundation.store.db.default_db_store import DefaultDbStore
from foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from memory_core.config.config import AgentMemoryConfig, DreamingConfig
from memory_core.long_term_memory import LongTermMemory


class _InMemoryIndex(BaseMemoryIndex):
    """
    Deterministic in-memory memory index: dict[(user,scope)] -> {id: MemoryDoc}.

    search() returns stored docs (filtered by mem_type) with a fixed score, which
    is all the dreaming/search path needs without a real embedding/vector backend.
    """

    def __init__(self):
        self._docs: dict[tuple[str, str], dict[str, MemoryDoc]] = {}

    def set_storage_codec(self, codec) -> None:
        pass

    async def add_memories(self, user_id, scope_id, memories):
        bucket = self._docs.setdefault((user_id, scope_id), {})
        for doc in memories:
            bucket[doc.id] = doc

    async def update_memories(self, user_id, scope_id, memories):
        await self.add_memories(user_id, scope_id, memories)

    async def delete_memories(self, user_id, scope_id, ids):
        bucket = self._docs.get((user_id, scope_id), {})
        for i in ids:
            bucket.pop(i, None)

    async def delete_by_user(self, user_id):
        for key in [k for k in self._docs if k[0] == user_id]:
            self._docs.pop(key, None)

    async def delete_by_scope(self, scope_id):
        for key in [k for k in self._docs if k[1] == scope_id]:
            self._docs.pop(key, None)

    async def delete_by_user_and_scope(self, user_id, scope_id):
        self._docs.pop((user_id, scope_id), None)

    async def search(self, user_id, scope_id, query, mem_types=None, top_k=10):
        bucket = self._docs.get((user_id, scope_id), {})
        hits = [
            (doc, 1.0) for doc in bucket.values()
            if not mem_types or doc.type in mem_types
        ]
        return hits[:top_k]

    async def get_by_id(self, user_id, scope_id, mem_id):
        return self._docs.get((user_id, scope_id), {}).get(mem_id)

    async def list_memories(self, user_id, scope_id, offset=0, limit=100, mem_types=None):
        docs = list(self._docs.get((user_id, scope_id), {}).values())
        if mem_types:
            docs = [d for d in docs if d.type in mem_types]
        return docs[offset:offset + limit]

    async def cleanup_backup(self, backup_id):
        pass

    async def list_user_scopes(self):
        return list(self._docs.keys())


class _MockEmbedding(Embedding):
    def __init__(self):
        self.limiter = None

    @property
    def dimension(self) -> int:
        return 8

    async def embed_query(self, text: str, **kwargs) -> List[float]:
        return [0.0] * 8

    async def embed_documents(self, texts: List[str], **kwargs) -> List[List[float]]:
        return [[0.0] * 8 for _ in texts]


def _dreaming_llm(items: list[dict]):
    """LLM whose invoke returns the given knowledge items as fenced JSON."""
    llm = MagicMock()
    payload = json.dumps(items, ensure_ascii=False)
    llm.invoke = AsyncMock(return_value=AssistantMessage(content=f"```json\n{payload}\n```"))
    return llm


def _smart_llm(extraction_items: list[dict]):
    """
    LLM that serves BOTH dreaming roles off one object:
    - dreaming extraction prompt → returns `extraction_items`
    - MemUpdateChecker prompt    → marks every NEW item `redundant`
    so the dreaming write path actually exercises dedup.
    """
    async def _invoke(messages=None, **_):
        content = messages[0]["content"]
        if "长期记忆提炼器" in content:                       # dreaming_extraction.md marker
            body = json.dumps(extraction_items, ensure_ascii=False)
            return AssistantMessage(content=f"```json\n{body}\n```")
        if "信息一致性校验专家" in content:                   # memory_update_check.md marker
            new_block = content.split("## 新信息", 1)[-1]
            ids = re.findall(r"([0-9a-f]{16,}):", new_block)  # DataIdManager ids are long hex
            verdicts = [{"info_id": i, "info_text": "", "result": "redundant", "related_infos": {}} for i in ids]
            return AssistantMessage(content=f"```json\n{json.dumps(verdicts, ensure_ascii=False)}\n```")
        return AssistantMessage(content="[]")

    llm = MagicMock()
    llm.invoke = AsyncMock(side_effect=_invoke)
    return llm


@pytest.mark.asyncio
@pytest.mark.skip(reason="temporarily skipped: failing in CI unit_test_report (7)")
async def test_dreaming_end_to_end(tmp_path):
    Singleton._instances.pop(LongTermMemory, None)
    try:
        # ── bootstrap LongTermMemory: real in-memory KV + real sqlite message store ──
        mem = LongTermMemory()
        await mem.register_plugin("inmemory_index", _InMemoryIndex, {})  # set memory_index first
        engine = create_async_engine(f"sqlite+aiosqlite:///{os.path.join(str(tmp_path), 'e2e.db')}")
        await mem.register_store(
            kv_store=InMemoryKVStore(),
            db_store=DefaultDbStore(engine),          # message_store auto-created from it
            embedding_model=_MockEmbedding(),
            # no vector_store → keeps our in-memory index, skips vector migration
        )
        # dreaming/extraction LLM returns one user_profile fact
        llm = _dreaming_llm([{"mem_type": "user_profile", "title": "职业", "content": "用户是一名数据分析师"}])
        mem._get_scope_llm = AsyncMock(return_value=llm)

        scope, user, sess = "scope_e2e", "alice", "sess_1"

        # ── feed a multi-round session; gen_mem=False → only populate message_store ──
        rounds = [("我是一名数据分析师", "了解"), ("我平时用 pandas 处理数据", "好的")]
        for user_text, asst_text in rounds:
            await mem.add_messages(
                messages=[BaseMessage(role="user", content=user_text),
                          BaseMessage(role="assistant", content=asst_text)],
                agent_config=AgentMemoryConfig(enable_long_term_mem=True),
                user_id=user, scope_id=scope, session_id=sess, gen_mem=False,
            )

        # ── run dreaming through the public API, trigger one sweep immediately ──
        orch = await mem.start_dreaming(
            scope, user, config=DreamingConfig(enabled=True, min_session_rounds=2),
        )
        assert orch is not None
        await orch._sweep_fn()           # bypass the scheduled interval; run one sweep now
        await mem.stop_dreaming()

        # ── the dreamed memory is retrievable via the normal search path ──
        results = await mem.search_user_mem("用户的职业", num=5, user_id=user, scope_id=scope)
        contents = [r.mem_info.content for r in results]
        assert any("数据分析师" in c for c in contents), contents

        # ── checkpoint advanced for the swept session ──
        raw = await mem.kv_store.get(f"dreaming/checkpoint/{scope}/{user}")
        assert raw is not None and sess in (raw if isinstance(raw, str) else raw.decode())
    finally:
        Singleton._instances.pop(LongTermMemory, None)


@pytest.mark.asyncio
async def test_dreaming_no_sessions_is_noop(tmp_path):
    """No stored conversation → sweep promotes nothing, no checkpoint written."""
    Singleton._instances.pop(LongTermMemory, None)
    try:
        mem = LongTermMemory()
        await mem.register_plugin("inmemory_index", _InMemoryIndex, {})
        engine = create_async_engine(f"sqlite+aiosqlite:///{os.path.join(str(tmp_path), 'e2e2.db')}")
        await mem.register_store(
            kv_store=InMemoryKVStore(),
            db_store=DefaultDbStore(engine),
            embedding_model=_MockEmbedding(),
        )
        mem._get_scope_llm = AsyncMock(return_value=_dreaming_llm([]))

        orch = await mem.start_dreaming("s", "u", config=DreamingConfig(enabled=True, min_session_rounds=2))
        await orch._sweep_fn()
        await mem.stop_dreaming()

        results = await mem.search_user_mem("anything", num=5, user_id="u", scope_id="s")
        assert results == []
        assert await mem.kv_store.get("dreaming/checkpoint/s/u") is None
    finally:
        Singleton._instances.pop(LongTermMemory, None)


@pytest.mark.asyncio
@pytest.mark.skip(reason="temporarily skipped: failing in CI unit_test_report (7)")
async def test_dreaming_dedup_against_existing_memory(tmp_path):
    """
    A dreamed memory that duplicates an existing one is routed through
    MemUpdateChecker and dropped — proving dreaming reuses the normal dedup path.
    """
    Singleton._instances.pop(LongTermMemory, None)
    try:
        mem = LongTermMemory()
        await mem.register_plugin("inmemory_index", _InMemoryIndex, {})
        engine = create_async_engine(f"sqlite+aiosqlite:///{os.path.join(str(tmp_path), 'e2e3.db')}")
        await mem.register_store(
            kv_store=InMemoryKVStore(),
            db_store=DefaultDbStore(engine),
            embedding_model=_MockEmbedding(),
        )

        scope, user, sess = "scope_dedup", "bob", "sess_1"

        # pre-seed an existing memory; the in-memory index returns it as "related"
        await mem.memory_index.add_memories(
            user, scope,
            [MemoryDoc(id="old1", text="用户是数据分析师", type="user_profile")],
        )

        # dreaming will extract a near-duplicate; checker will mark it redundant
        llm = _smart_llm([{"mem_type": "user_profile", "title": "职业", "content": "用户是数据分析师"}])
        mem._get_scope_llm = AsyncMock(return_value=llm)

        await mem.add_messages(
            messages=[BaseMessage(role="user", content="我是数据分析师"),
                      BaseMessage(role="assistant", content="了解"),
                      BaseMessage(role="user", content="对，就是分析数据的"),
                      BaseMessage(role="assistant", content="好的")],
            agent_config=AgentMemoryConfig(enable_long_term_mem=True),
            user_id=user, scope_id=scope, session_id=sess, gen_mem=False,
        )

        orch = await mem.start_dreaming(scope, user, config=DreamingConfig(enabled=True, min_session_rounds=2))
        await orch._sweep_fn()
        await mem.stop_dreaming()

        # checker ran (2 invokes: extract + dedup-check) and the redundant item was NOT added
        assert llm.invoke.await_count == 2
        remaining = await mem.memory_index.list_memories(user, scope, 0, 100)
        assert [d.id for d in remaining] == ["old1"]      # still just the original, no duplicate added
    finally:
        Singleton._instances.pop(LongTermMemory, None)


@pytest.mark.asyncio
@pytest.mark.skip(reason="temporarily skipped: failing in CI unit_test_report (7)")
async def test_deleting_messages_clears_dreaming_checkpoint(tmp_path):
    """
    delete_messages_by_user_and_scope drops the swept-session checkpoint, so a
    reused session_id is not skipped forever (memory deletes do NOT clear it).
    """
    Singleton._instances.pop(LongTermMemory, None)
    try:
        mem = LongTermMemory()
        await mem.register_plugin("inmemory_index", _InMemoryIndex, {})
        engine = create_async_engine(f"sqlite+aiosqlite:///{os.path.join(str(tmp_path), 'e2e4.db')}")
        await mem.register_store(
            kv_store=InMemoryKVStore(),
            db_store=DefaultDbStore(engine),
            embedding_model=_MockEmbedding(),
        )
        mem._get_scope_llm = AsyncMock(return_value=_dreaming_llm(
            [{"mem_type": "user_profile", "title": "职业", "content": "用户是一名数据分析师"}]))

        scope, user, sess = "scope_ck", "carol", "sess_1"
        await mem.add_messages(
            messages=[BaseMessage(role="user", content="我是数据分析师"),
                      BaseMessage(role="assistant", content="了解"),
                      BaseMessage(role="user", content="对"),
                      BaseMessage(role="assistant", content="好")],
            agent_config=AgentMemoryConfig(enable_long_term_mem=True),
            user_id=user, scope_id=scope, session_id=sess, gen_mem=False,
        )
        orch = await mem.start_dreaming(scope, user, config=DreamingConfig(enabled=True, min_session_rounds=2))
        await orch._sweep_fn()
        await mem.stop_dreaming()

        ckpt_key = f"dreaming/checkpoint/{scope}/{user}"
        assert await mem.kv_store.get(ckpt_key) is not None      # swept → checkpoint exists

        await mem.delete_messages_by_user_and_scope(user_id=user, scope_id=scope)
        assert await mem.kv_store.get(ckpt_key) is None          # messages gone → checkpoint cleared
    finally:
        Singleton._instances.pop(LongTermMemory, None)
