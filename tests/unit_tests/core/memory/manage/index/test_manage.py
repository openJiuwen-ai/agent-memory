#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
# Tests reset the Singleton; no public API exists for this, so direct
# access to Singleton._instances is unavoidable here.
# pylint: disable=protected-access
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

os.environ['HF_ENDPOINT'] = "https://hf-mirror.com"
from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import FragmentMemoryManager
from jiuwen_memory.memory_core.manage.index.variable_manager import VariableManager
from jiuwen_memory.memory_core.manage.index.write_manager import WriteManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import FragmentMemoryUnit, \
    VariableUnit, MemoryType
from jiuwen_memory.memory_core.config.config import MemoryEngineConfig
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
from jiuwen_memory.foundation.store.base_db_store import BaseDbStore
from jiuwen_memory.foundation.store.base_embedding import Embedding
from jiuwen_memory.foundation.store.base_memory_index import BaseMemoryIndex, MemoryDoc
from jiuwen_memory.foundation.store.base_vector_store import BaseVectorStore
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore


class MockMemoryIndex(BaseMemoryIndex):
    """In-memory mock implementation of BaseMemoryIndex for testing."""

    def __init__(self):
        self._data: dict[str, dict[str, dict[str, MemoryDoc]]] = {}
        self._schema_version = 0
        self._backups: dict[str, dict[str, Any]] = {}

    def set_storage_codec(self, codec) -> None:
        pass

    def _ensure_user_scope(self, user_id: str, scope_id: str):
        if user_id not in self._data:
            self._data[user_id] = {}
        if scope_id not in self._data[user_id]:
            self._data[user_id][scope_id] = {}

    async def add_memories(self, user_id: str, scope_id: str, memories: list[MemoryDoc]):
        self._ensure_user_scope(user_id, scope_id)
        for doc in memories:
            self._data[user_id][scope_id][doc.id] = doc

    async def update_memories(self, user_id: str, scope_id: str, memories: list[MemoryDoc]):
        """Update memories by deleting old ones then adding new ones."""
        if not memories:
            return
        ids = [m.id for m in memories]
        await self.delete_memories(user_id, scope_id, ids)
        await self.add_memories(user_id, scope_id, memories)

    async def delete_memories(self, user_id: str, scope_id: str, ids: list[str]):
        if user_id in self._data and scope_id in self._data[user_id]:
            for mid in ids:
                self._data[user_id][scope_id].pop(mid, None)

    async def delete_by_user(self, user_id: str):
        self._data.pop(user_id, None)

    async def delete_by_scope(self, scope_id: str):
        for uid in list(self._data.keys()):
            self._data[uid].pop(scope_id, None)

    async def delete_by_user_and_scope(self, user_id: str, scope_id: str):
        if user_id in self._data:
            self._data[user_id].pop(scope_id, None)

    async def search(self, user_id: str, scope_id: str, query: str,
                     mem_types: list[str] | None = None, top_k: int = 10) -> list[tuple[MemoryDoc, float]]:
        if user_id not in self._data or scope_id not in self._data[user_id]:
            return []
        results = []
        for doc in self._data[user_id][scope_id].values():
            if mem_types and doc.type not in mem_types:
                continue
            score = 1.0 if query in doc.text else 0.5
            results.append((doc, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    async def get_by_id(self, user_id: str, scope_id: str, mem_id: str) -> MemoryDoc | None:
        if user_id in self._data and scope_id in self._data[user_id]:
            return self._data[user_id][scope_id].get(mem_id)
        return None

    async def list_memories(self, user_id: str, scope_id: str, offset: int = 0, limit: int = 100, mem_types: list[str] | None = None) -> list[MemoryDoc]:
        if user_id not in self._data or scope_id not in self._data[user_id]:
            return []
        docs = sorted(self._data[user_id][scope_id].values(), key=lambda d: d.timestamp, reverse=True)
        if mem_types:
            docs = [d for d in docs if d.type in mem_types]
        return docs[offset:offset + limit]

    def get_schema_version(self) -> int:
        return self._schema_version

    def update_schema_version(self, version: int) -> None:
        self._schema_version = version

    async def create_backup(self) -> str:
        import uuid
        bid = str(uuid.uuid4())
        self._backups[bid] = {"schema_version": self._schema_version}
        return bid

    async def restore_backup(self, backup_id: str) -> None:
        if backup_id not in self._backups:
            raise ValueError(f"Backup {backup_id} not found")
        self._schema_version = self._backups[backup_id]["schema_version"]

    async def cleanup_backup(self, backup_id: str) -> None:
        self._backups.pop(backup_id, None)

    async def list_user_scopes(self) -> list[tuple[str, str]]:
        scopes = []
        for uid, scope_dict in self._data.items():
            for sid in scope_dict.keys():
                scopes.append((uid, sid))
        return scopes


class MockVectorStore(BaseVectorStore):
    """Minimal mock vector store that satisfies LongTermMemory.register_store."""

    async def create_collection(self, *a, **kw):
        pass

    async def delete_collection(self, *a, **kw):
        pass

    async def get_schema(self, *a, **kw):
        return {}

    async def add_docs(self, *a, **kw):
        return []

    async def search(self, *a, **kw):
        return []

    async def delete_docs_by_ids(self, *a, **kw):
        pass

    async def delete_docs_by_filters(self, *a, **kw):
        pass

    async def collection_exists(self, *a, **kw):
        return False

    async def list_collection_names(self, *a, **kw):
        return []

    async def get_collection_metadata(self, *a, **kw):
        return {}

    async def update_collection_metadata(self, *a, **kw):
        pass

    async def update_schema(self, *a, **kw):
        pass


class MockDbStore(BaseDbStore):
    """Mock db store: register_store/set_config only check db_store is not None;
    migrations run against an empty registry, so the MagicMock engine is never
    actually exercised. Avoids any sqlite file I/O in this unit test.
    """

    def get_async_engine(self):
        mock_engine = MagicMock()
        mock_engine.begin = MagicMock(return_value=AsyncMock())
        return mock_engine


class MockEmbedding(Embedding):
    """Deterministic embedding so SimpleMemoryIndex-backed LongTermMemory can init without a model."""

    def __init__(self):
        self.limiter = None

    @property
    def dimension(self) -> int:
        return 8

    async def embed_query(self, text: str, **kwargs) -> list[float]:
        return [0.0] * self.dimension

    async def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


@pytest.fixture(autouse=True)
def _reset_ltm_singleton():
    """Reset the LongTermMemory singleton around each test for isolation."""
    Singleton._instances.pop(LongTermMemory, None)
    yield
    Singleton._instances.pop(LongTermMemory, None)


@pytest_asyncio.fixture(name="long_term_memory_engine")
async def long_term_memory_engine_fixture():
    """A LongTermMemory wired with InMemoryKVStore + mock stores.

    Lets update_variables/get_variables run through the real public API
    (DistributedLock, _validate_id, VariableManager) without an LLM, external
    services, or sqlite file I/O.
    """
    engine = LongTermMemory()
    await engine.register_store(
        kv_store=InMemoryKVStore(),
        vector_store=MockVectorStore(),
        db_store=MockDbStore(),
        embedding_model=MockEmbedding(),
    )
    engine.set_config(MemoryEngineConfig(crypto_key=b"test_crypto_key_1234560000000000"))
    return engine


class TestManage:
    @pytest.mark.asyncio
    async def test_basic(self):
        mock_kv_store = InMemoryKVStore()
        mock_memory_index = MockMemoryIndex()

        user_profile_manager = FragmentMemoryManager(
            memory_index=mock_memory_index,
            crypto_key=b""
        )
        variable_manager = VariableManager(mock_kv_store, b"")
        managers = {MemoryType.USER_PROFILE.value: user_profile_manager, MemoryType.VARIABLE.value: variable_manager}
        write_manager = WriteManager(managers, mock_memory_index)
        test_all_data = [
            {"mem_id": "1000", "mem_type": MemoryType.USER_PROFILE, "content": "用户非常喜欢川菜，尤其是水煮鱼和麻婆豆腐"},
            {"mem_id": "1001", "mem_type": MemoryType.USER_PROFILE, "content": "用户的职业是软件工程师，居住在北京市"},
            {"mem_id": "1002", "mem_type": MemoryType.USER_PROFILE, "content": "用户的副业是抖音直播"},
            {"mem_id": "1003", "mem_type": MemoryType.USER_PROFILE, "content": "用户的银行账户余额为10000元"},
            {"mem_id": "1004", "mem_type": MemoryType.USER_PROFILE, "content": "用户的朋友圈中有50个好友"},
            {"mem_id": "1005", "mem_type": MemoryType.USER_PROFILE, "content": "用户的宠物是一只金毛犬"},
        ]
        test_all_data1 = [
            {"mem_id": "019e0ad3b5acb22c931f1010", "mem_type": MemoryType.USER_PROFILE,
             "content": "用户喜欢打篮球和阅读历史小说"},
            {"mem_id": "019e0ad3b5acb22c931f1011", "mem_type": MemoryType.USER_PROFILE,
             "content": "用户的生日是1990年1月1日"},
            {"mem_id": "019e0ad3b5acb22c931f1012", "mem_type": MemoryType.USER_PROFILE,
             "content": "用户的汽车型号是特斯拉Model 3"},
            {"mem_id": "019e0ad3b5acb22c931f1013", "mem_type": MemoryType.USER_PROFILE,
             "content": "用户在Twitter上有200个关注者"},
        ]

        for item in test_all_data:
            mem_unit = FragmentMemoryUnit(**item)
            await write_manager.add_memories("usrZH2025", "fitnesstrackerv3",
                                             {mem_unit.mem_type.value: [mem_unit]}, None)
            mem_unit = VariableUnit(variable_name=item['mem_type'],
                                    variable_mem=item['content'])
            await write_manager.add_memories("usrZH2025", "fitnesstrackerv3",
                                             {mem_unit.mem_type.value: [mem_unit]}, None)

        for item in test_all_data1:
            mem_unit = FragmentMemoryUnit(**item)
            await write_manager.add_memories("usrZH2026", "fitnesstrackerv3",
                                             {mem_unit.mem_type.value: [mem_unit]}, None)
            mem_unit = VariableUnit(variable_name=item['mem_type'],
                                    variable_mem=item['content'])
            await write_manager.add_memories("usrZH2026", "fitnesstrackerv3",
                                             {mem_unit.mem_type.value: [mem_unit]}, None)

        query = "用户的职业"
        res = await user_profile_manager.search("usrZH2025", "fitnesstrackerv3", query, 5)
        assert len(res) == 5

        await user_profile_manager.update("usrZH2025", "fitnesstrackerv3", res[0]['id'],
                                          "用户不是软件工程师，是系统")
        ret = await user_profile_manager.get("usrZH2025", "fitnesstrackerv3", res[0]['id'])
        assert ret['mem'] == "用户不是软件工程师，是系统"

        res = await user_profile_manager.list_fragment_memories("usrZH2025", "fitnesstrackerv3", 0, 10)
        assert len(res) == 6
        for rr in res[0:2]:
            await write_manager.delete_mem_by_id("usrZH2025", "fitnesstrackerv3", rr["id"])

        res = await user_profile_manager.search("usrZH2025", "fitnesstrackerv3", query, 5)
        assert len(res) == 4
        await write_manager.delete_mem_by_user_id("usrZH2026", "fitnesstrackerv3")
        res = await user_profile_manager.search("usrZH2026", "fitnesstrackerv3", query, 5)
        assert len(res) == 0

    @pytest.mark.asyncio
    async def test_update_user_variable_upsert(self):
        """update_user_variable 为 upsert 语义：name 不存在时插入，存在时覆盖。

        回归用例：旧实现仅更新已存在变量，缺失 name 会静默 no-op，
        导致 update_variables 对首次写入静默失败。
        """
        mock_kv_store = InMemoryKVStore()
        variable_manager = VariableManager(mock_kv_store, b"")

        # 1) 首次写入（name 不在 kv 里）——应插入而非静默失败
        await variable_manager.update_user_variable(
            user_id="usrUpsert", scope_id="scope1", var_name="lang", var_mem="zh-CN"
        )
        got = await variable_manager.query_variable(user_id="usrUpsert", scope_id="scope1", name="lang")
        assert got.get("lang") == "zh-CN", "首次写入应插入变量"

        # 2) 同 name 再次写入——应覆盖
        await variable_manager.update_user_variable(
            user_id="usrUpsert", scope_id="scope1", var_name="lang", var_mem="en-US"
        )
        got = await variable_manager.query_variable(user_id="usrUpsert", scope_id="scope1", name="lang")
        assert got.get("lang") == "en-US", "已存在变量应被覆盖"

        # 3) 不同 name 并存——互不干扰
        await variable_manager.update_user_variable(
            user_id="usrUpsert", scope_id="scope1", var_name="theme", var_mem="dark"
        )
        all_vars = await variable_manager.query_variable(user_id="usrUpsert", scope_id="scope1")
        assert all_vars.get("lang") == "en-US"
        assert all_vars.get("theme") == "dark"

    @pytest.mark.asyncio
    async def test_update_variables_first_write_queryable(self, long_term_memory_engine):
        """对外接口 LongTermMemory.update_variables：首次写入 kv 中不存在的 name 后可被取回。

        回归用例（对应 PR 标题）：旧实现 VariableManager.update_user_variable 仅更新
        已存在变量，缺失 name 会静默 no-op，导致 update_variables 对首次写入静默失败。
        test_update_user_variable_upsert 已在 VariableManager 层覆盖 upsert 语义；本例
        把断言下沉到对外 API，确认整条链路（update_variables → DistributedLock →
        VariableManager.update_user_variable → get_variables）对全新 name 的写入可见。
        """
        engine = long_term_memory_engine
        user_id = "usrUpdateVars"
        scope_id = "scope_update_vars"

        # 前置：该 name 在 kv 里尚不存在
        before = await engine.get_variables(names=["newkey"], user_id=user_id, scope_id=scope_id)
        assert before.get("newkey") in (None, ""), "前置：newkey 应尚未写入"

        # 首次写入一个 kv 中不存在的 name
        await engine.update_variables(
            variables={"newkey": "first-value"},
            user_id=user_id,
            scope_id=scope_id,
        )

        # 写后立即能取到——回归断言
        after = await engine.get_variables(names=["newkey"], user_id=user_id, scope_id=scope_id)
        assert after.get("newkey") == "first-value", "首次写入的 name 应可被 get_variables 取回"

        # 全量 get_variables 也应包含该 name
        all_vars = await engine.get_variables(user_id=user_id, scope_id=scope_id)
        assert all_vars.get("newkey") == "first-value", "全量查询应包含首次写入的 name"
