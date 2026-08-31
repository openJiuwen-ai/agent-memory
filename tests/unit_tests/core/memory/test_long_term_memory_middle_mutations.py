import asyncio
from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.exception.errors import BaseError
from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.foundation.llm import BaseMessage
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
from jiuwen_memory.memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
from jiuwen_memory.memory_core.manage.index.write_manager import WriteManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType


class FakeKVStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def exclusive_set(self, key: str, value: str, expiry: int | None = None) -> bool:
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def renew_exclusive(self, key: str, value: str, expiry: int | None = None) -> bool:
        return self.values.get(key) == value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FakeScopeUserMappingManager:
    def __init__(self, users: list[str]) -> None:
        self.users = users
        self.deleted_scopes: list[str] = []

    async def get_by_scope_id(self, scope_id: str) -> list[dict[str, str]]:
        return [{"user_id": user_id, "scope_id": scope_id} for user_id in self.users]

    async def delete_by_scope_id(self, scope_id: str) -> bool:
        self.deleted_scopes.append(scope_id)
        return True


class FakeWriteManager:
    def __init__(self, updated: bool = True) -> None:
        self.updated = updated
        self.delete_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, str, str, object | None]] = []

    async def delete_mem_by_user_id(self, user_id: str, scope_id: str, **kwargs) -> bool:
        self.delete_calls.append((user_id, scope_id))
        return True

    async def update_mem_by_id(self, user_id: str, scope_id: str, mem_id: str, memory: str, **kwargs) -> bool:
        self.update_calls.append((user_id, scope_id, mem_id, memory, kwargs.get("semantic_store")))
        return self.updated


class FakeMiddleWriteManager:
    def __init__(self, updated: bool = True) -> None:
        self.updated = updated
        self.delete_calls: list[tuple[str, str, object | None]] = []
        self.update_calls: list[tuple[str, str, str, str, object | None]] = []

    async def delete_mem_by_user_id(self, user_id: str, scope_id: str, **kwargs) -> bool:
        self.delete_calls.append((user_id, scope_id, kwargs.get("semantic_store")))
        return True

    async def update_mem_by_id(self, user_id: str, scope_id: str, mem_id: str, memory: str, **kwargs) -> bool:
        self.update_calls.append((user_id, scope_id, mem_id, memory, kwargs.get("semantic_store")))
        return self.updated


class FakeMemoryIndexMissing:
    async def get_by_id(self, user_id: str, scope_id: str, mem_id: str):
        return None


class FakeMemoryIndexLongTerm:
    def __init__(self, mem_type: str) -> None:
        self.mem_type = mem_type

    async def get_by_id(self, user_id: str, scope_id: str, mem_id: str):
        return type("MemoryDoc", (), {"type": self.mem_type})()


class FakeMiddleUpdateManager:
    def __init__(self, updated: bool = True) -> None:
        self.updated = updated
        self.update_calls: list[tuple[str, str, str, str, object | None]] = []
        self.delete_calls: list[tuple[str, str, str, object | None]] = []

    async def update(self, user_id: str, scope_id: str, mem_id: str, new_memory: str, **kwargs) -> bool:
        self.update_calls.append((user_id, scope_id, mem_id, new_memory, kwargs.get("semantic_store")))
        return self.updated

    async def delete(self, user_id: str, scope_id: str, mem_id: str, **kwargs) -> bool:
        self.delete_calls.append((user_id, scope_id, mem_id, kwargs.get("semantic_store")))
        return self.updated


class FakeLongTermUpdateManager:
    def __init__(self, updated: bool = True) -> None:
        self.updated = updated
        self.update_calls: list[tuple[str, str, str, str, object | None]] = []
        self.delete_calls: list[tuple[str, str, str, object | None]] = []

    async def update(self, user_id: str, scope_id: str, mem_id: str, new_memory: str, **kwargs) -> bool:
        self.update_calls.append((user_id, scope_id, mem_id, new_memory, kwargs.get("semantic_store")))
        return self.updated

    async def delete(self, user_id: str, scope_id: str, mem_id: str, **kwargs) -> bool:
        self.delete_calls.append((user_id, scope_id, mem_id, kwargs.get("semantic_store")))
        return self.updated


class FakeMessageManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.page_calls: list[dict] = []
        self.messages: list[tuple[BaseMessage, datetime, str]] = []

    async def delete_by_user_and_scope(self, user_id: str, scope_id: str) -> bool:
        self.calls.append((user_id, scope_id))
        return True

    async def get(self, user_id: str = None, scope_id: str = None, session_id: str = None,
                  message_len: int = 10) -> list[tuple[BaseMessage, datetime, str]]:
        return self.messages[:message_len]

    async def get_page(self, user_id: str = None, scope_id: str = None, session_id: str = None,
                       offset: int = 0, limit: int = 10) -> list[tuple[BaseMessage, datetime, str]]:
        self.page_calls.append({
            "user_id": user_id,
            "scope_id": scope_id,
            "offset": offset,
            "limit": limit,
        })
        oldest_first = sorted(self.messages, key=lambda item: (item[1], item[2]))
        return oldest_first[offset:offset + limit]


class FakeSearchManager:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls: list[dict] = []

    async def list_user_mem(self, user_id: str, scope_id: str, nums: int, pages: int, mem_type: str = None,
                            *, filters=None):
        self.calls.append({
            "user_id": user_id,
            "scope_id": scope_id,
            "nums": nums,
            "pages": pages,
            "mem_type": mem_type,
            "filters": filters,
        })
        filtered = [item for item in self.items if mem_type is None or item.get("mem_type") == mem_type]
        start_idx = nums * (pages - 1)
        return filtered[start_idx:start_idx + nums]

    async def list_user_mem_by_offset(self, user_id: str, scope_id: str, offset: int, limit: int,
                                      mem_type: str = None, *, filters=None):
        self.calls.append({
            "user_id": user_id,
            "scope_id": scope_id,
            "offset": offset,
            "limit": limit,
            "mem_type": mem_type,
            "filters": filters,
        })
        filtered = [item for item in self.items if mem_type is None or item.get("mem_type") == mem_type]
        oldest_first = sorted(filtered, key=lambda item: (item.get("timestamp"), item["id"]))
        return oldest_first[offset:offset + limit]


class FakeSemanticStore:
    def __init__(self) -> None:
        self.deleted_docs: list[tuple[list[str], str]] = []
        self.added_docs: list[dict] = []
        self.deleted_tables: list[str] = []

    async def delete_docs(self, ids: list[str], table_name: str) -> None:
        self.deleted_docs.append((ids, table_name))

    async def add_docs(self, docs, table_name: str, scope_id: str | None = None, 
                       is_middle: bool | None = False) -> bool:
        self.added_docs.append({
            "docs": docs,
            "table_name": table_name,
            "scope_id": scope_id,
            "is_middle": is_middle,
        })
        return True

    async def delete_table(self, table_name: str) -> None:
        self.deleted_tables.append(table_name)


class FakeVectorStore:
    pass


async def sleepy_task() -> None:
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        raise


@pytest.fixture(autouse=True)
def reset_long_term_memory_singleton():
    Singleton._instances.pop(LongTermMemory, None)
    yield
    Singleton._instances.pop(LongTermMemory, None)


@pytest.mark.asyncio
async def test_delete_mem_by_scope_deletes_middle_memory_and_messages():
    scope_id = "local_scope_delete_middle"
    users = ["local_user_a", "local_user_b"]
    semantic_store = object()

    memory = LongTermMemory()
    memory.scope_user_mapping_manager = FakeScopeUserMappingManager(users)
    memory.write_manager = FakeWriteManager()
    memory.middle_write_manager = FakeMiddleWriteManager()
    memory.message_manager = FakeMessageManager()
    memory.vector_store = FakeVectorStore()
    memory.kv_store = FakeKVStore()

    async def fake_create_semantic_store(scope_id_arg: str) -> object:
        assert scope_id_arg == scope_id
        return semantic_store

    memory._create_semantic_store_with_embedding = fake_create_semantic_store

    running_task = asyncio.create_task(sleepy_task())
    memory._middle_memory_tasks[(scope_id, users[0])] = running_task

    assert await memory.delete_mem_by_scope(scope_id) is True

    assert memory.write_manager.delete_calls == [(user_id, scope_id) for user_id in users]
    assert [
        (user_id, call_scope, store is semantic_store)
        for user_id, call_scope, store in memory.middle_write_manager.delete_calls
    ] == [(user_id, scope_id, True) for user_id in users]
    assert memory.message_manager.calls == [
        ("local_user_a", "local_scope_delete_middle"),
        ("local_user_a", "middle_term_memory:local_scope_delete_middle:local_user_a"),
        ("local_user_b", "local_scope_delete_middle"),
        ("local_user_b", "middle_term_memory:local_scope_delete_middle:local_user_b"),
    ]
    assert memory.scope_user_mapping_manager.deleted_scopes == [scope_id]
    assert running_task.cancelled()
    assert (scope_id, users[0]) not in memory._middle_memory_tasks


@pytest.mark.asyncio
async def test_long_term_memory_update_passes_semantic_store_to_write_manager():
    user_id = "local_update_user"
    scope_id = "local_update_scope"
    mem_id = "msg_middle_001"
    new_memory = "我在北京工作"
    semantic_store = object()

    memory = LongTermMemory()
    memory.kv_store = FakeKVStore()
    memory.write_manager = FakeWriteManager(updated=True)
    memory.vector_store = FakeVectorStore()

    async def fake_apply_scope_embedding(scope_id_arg: str) -> None:
        assert scope_id_arg == scope_id

    async def fake_create_semantic_store(scope_id_arg: str) -> object:
        assert scope_id_arg == scope_id
        return semantic_store

    memory._apply_scope_embedding = fake_apply_scope_embedding
    memory._create_semantic_store_with_embedding = fake_create_semantic_store

    await memory.update_mem_by_id(
        mem_id=mem_id,
        memory=new_memory,
        user_id=user_id,
        scope_id=scope_id,
    )

    assert memory.write_manager.update_calls == [
        (user_id, scope_id, mem_id, new_memory, semantic_store),
    ]


@pytest.mark.asyncio
async def test_write_manager_update_converts_to_insert_when_mem_type_missing():
    user_id = "local_update_user"
    scope_id = "local_update_scope"
    mem_id = "msg_middle_001"
    new_memory = "我在北京工作"
    semantic_store = object()

    long_term_manager = FakeLongTermUpdateManager(updated=True)
    middle_manager = FakeMiddleUpdateManager(updated=True)
    write_manager = WriteManager(
        managers={"semantic_memory": long_term_manager},
        memory_index=FakeMemoryIndexMissing(),
        middle_manager=middle_manager,
    )

    result = await write_manager.update_mem_by_id(
        user_id=user_id,
        scope_id=scope_id,
        mem_id=mem_id,
        memory=new_memory,
        semantic_store=semantic_store,
    )

    assert result is True
    assert long_term_manager.update_calls == [
        (user_id, scope_id, mem_id, new_memory, semantic_store),
    ]
    assert middle_manager.update_calls == []


@pytest.mark.asyncio
async def test_write_manager_update_uses_long_term_manager_when_mem_type_exists():
    user_id = "local_update_user"
    scope_id = "local_update_scope"
    mem_id = "msg_long_001"
    new_memory = "我在北京工作"

    semantic_store = object()
    long_term_manager = FakeLongTermUpdateManager(updated=True)
    middle_manager = FakeMiddleUpdateManager(updated=True)
    write_manager = WriteManager(
        managers={"semantic_memory": long_term_manager},
        memory_index=FakeMemoryIndexLongTerm("semantic_memory"),
        middle_manager=middle_manager,
    )

    result = await write_manager.update_mem_by_id(
        user_id=user_id,
        scope_id=scope_id,
        mem_id=mem_id,
        memory=new_memory,
        semantic_store=semantic_store,
    )

    assert result is True
    assert long_term_manager.update_calls == [
        (user_id, scope_id, mem_id, new_memory, semantic_store),
    ]
    assert middle_manager.update_calls == []


@pytest.mark.asyncio
async def test_write_manager_delete_falls_back_to_middle_memory():
    user_id = "local_delete_user"
    scope_id = "local_delete_scope"
    mem_id = "msg_middle_001"
    semantic_store = object()

    write_manager = WriteManager(
        managers={},
        memory_index=FakeMemoryIndexMissing(),
        middle_manager=FakeMiddleUpdateManager(updated=True),
    )

    result = await write_manager.delete_mem_by_id(
        user_id=user_id,
        scope_id=scope_id,
        mem_id=mem_id,
        semantic_store=semantic_store,
    )

    assert result is True
    assert write_manager.middle_manager.delete_calls == [
        (user_id, scope_id, mem_id, semantic_store),
    ]


@pytest.mark.asyncio
async def test_write_manager_delete_uses_long_term_manager_when_mem_type_exists():
    user_id = "local_delete_user"
    scope_id = "local_delete_scope"
    mem_id = "msg_long_001"
    semantic_store = object()

    long_term_manager = FakeLongTermUpdateManager(updated=True)
    middle_manager = FakeMiddleUpdateManager(updated=True)
    write_manager = WriteManager(
        managers={"semantic_memory": long_term_manager},
        memory_index=FakeMemoryIndexLongTerm("semantic_memory"),
        middle_manager=middle_manager,
    )

    result = await write_manager.delete_mem_by_id(
        user_id=user_id,
        scope_id=scope_id,
        mem_id=mem_id,
        semantic_store=semantic_store,
    )

    assert result is True
    assert long_term_manager.delete_calls == [
        (user_id, scope_id, mem_id, semantic_store),
    ]
    assert middle_manager.delete_calls == []


@pytest.mark.asyncio
async def test_middle_memory_manager_update_replaces_vector_doc():
    manager = MiddleTermMemoryManager(memory_index=object(), crypto_key=b"")
    semantic_store = FakeSemanticStore()

    result = await manager.update(
        user_id="u1",
        scope_id="s1",
        mem_id="msg_001",
        new_memory="我在北京工作",
        semantic_store=semantic_store,
    )

    assert result is True
    assert len(semantic_store.deleted_docs) == 1
    deleted_ids, deleted_table = semantic_store.deleted_docs[0]
    assert deleted_ids == ["msg_001"]
    assert "middle_term_memory" in deleted_table

    assert len(semantic_store.added_docs) == 1
    added = semantic_store.added_docs[0]
    assert added["table_name"] == deleted_table
    assert added["scope_id"] == "s1"
    assert added["is_middle"] is True
    assert added["docs"][0][0] == "msg_001"
    assert added["docs"][0][1] == "我在北京工作"


@pytest.mark.asyncio
async def test_get_user_mem_by_page_returns_middle_memory_when_type_filter_middle():
    memory = LongTermMemory()
    memory.search_manager = FakeSearchManager([])
    memory.message_manager = FakeMessageManager()
    timestamp = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    memory.message_manager.messages = [
        (BaseMessage(role="user", content="我在上海工作"), timestamp, "msg_middle_001"),
    ]

    results = await memory.get_user_mem_by_page(
        user_id="u1",
        scope_id="s1",
        page_size=10,
        page_idx=1,
        memory_type=MemoryType.MIDDLE_TERM_MEMORY,
    )

    assert len(results) == 1
    assert results[0].mem_id == "msg_middle_001"
    assert results[0].content == "我在上海工作"
    assert results[0].type == MemoryType.MIDDLE_TERM_MEMORY
    assert results[0].timestamp == timestamp
    assert memory.search_manager.calls == []


@pytest.mark.asyncio
async def test_get_user_mem_by_page_unknown_merges_long_and_middle_memory():
    long_timestamp = datetime(2026, 7, 21, 11, 0, tzinfo=timezone.utc)
    middle_timestamp = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    memory = LongTermMemory()
    memory.search_manager = FakeSearchManager([
        {
            "id": "long_001",
            "mem": "长期记忆",
            "mem_type": MemoryType.SEMANTIC_MEMORY.value,
            "timestamp": long_timestamp,
        }
    ])
    memory.message_manager = FakeMessageManager()
    memory.message_manager.messages = [
        (BaseMessage(role="user", content="中期记忆"), middle_timestamp, "msg_middle_001"),
    ]

    results = await memory.get_user_mem_by_page(
        user_id="u1",
        scope_id="s1",
        page_size=10,
        page_idx=1,
        memory_type=MemoryType.UNKNOWN,
    )

    assert [(item.mem_id, item.content, item.type) for item in results] == [
        ("long_001", "长期记忆", MemoryType.SEMANTIC_MEMORY),
        ("msg_middle_001", "中期记忆", MemoryType.MIDDLE_TERM_MEMORY),
    ]
    assert memory.search_manager.calls == [{
        "user_id": "u1",
        "scope_id": "s1",
        "offset": 0,
        "limit": 10,
        "mem_type": None,
        "filters": None,
    }]


@pytest.mark.asyncio
async def test_get_user_mem_by_page_unknown_has_no_cross_page_duplicates():
    oldest_first = [
        {
            "id": f"long_{idx:03d}",
            "mem": f"长期记忆 {idx}",
            "mem_type": MemoryType.SEMANTIC_MEMORY.value,
            "timestamp": datetime(2026, 7, 21, idx, 0, tzinfo=timezone.utc),
        }
        for idx in range(10)
    ]
    memory = LongTermMemory()
    # Production indexes list newest-first.  The old implementation fetched
    # an expanding newest-N prefix, reversed it, and returned duplicate pages.
    memory.search_manager = FakeSearchManager(list(reversed(oldest_first)))
    memory.message_manager = FakeMessageManager()

    pages = [
        await memory.get_user_mem_by_page(
            user_id="u1",
            scope_id="s1",
            page_size=3,
            page_idx=page_idx,
            memory_type=MemoryType.UNKNOWN,
        )
        for page_idx in range(1, 6)
    ]

    assert [[item.mem_id for item in page] for page in pages] == [
        ["long_000", "long_001", "long_002"],
        ["long_003", "long_004", "long_005"],
        ["long_006", "long_007", "long_008"],
        ["long_009"],
        [],
    ]
    all_ids = [item.mem_id for page in pages for item in page]
    assert len(all_ids) == len(set(all_ids)) == 10


@pytest.mark.asyncio
async def test_get_user_mem_by_page_unknown_orders_same_timestamp_by_mem_id():
    timestamp = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    memory = LongTermMemory()
    memory.search_manager = FakeSearchManager([
        {
            "id": "b-long",
            "mem": "长期记忆 B",
            "mem_type": MemoryType.SEMANTIC_MEMORY.value,
            "timestamp": timestamp,
        },
        {
            "id": "a-long",
            "mem": "长期记忆 A",
            "mem_type": MemoryType.SEMANTIC_MEMORY.value,
            "timestamp": timestamp,
        },
    ])
    memory.message_manager = FakeMessageManager()
    memory.message_manager.messages = [
        (BaseMessage(role="user", content="中期记忆 D"), timestamp, "d-middle"),
        (BaseMessage(role="user", content="中期记忆 C"), timestamp, "c-middle"),
    ]

    pages = [
        await memory.get_user_mem_by_page(
            user_id="u1",
            scope_id="s1",
            page_size=2,
            page_idx=page_idx,
            memory_type=MemoryType.UNKNOWN,
        )
        for page_idx in (1, 2)
    ]

    assert [[item.mem_id for item in page] for page in pages] == [
        ["a-long", "b-long"],
        ["c-middle", "d-middle"],
    ]
    all_ids = [item.mem_id for page in pages for item in page]
    assert len(all_ids) == len(set(all_ids)) == 4


@pytest.mark.asyncio
async def test_get_user_mem_by_page_middle_has_no_cross_page_duplicates():
    oldest_first = [
        (
            BaseMessage(role="user", content=f"中期记忆 {idx}"),
            datetime(2026, 7, 21, idx, 0, tzinfo=timezone.utc),
            f"middle_{idx:03d}",
        )
        for idx in range(10)
    ]
    memory = LongTermMemory()
    memory.search_manager = FakeSearchManager([])
    memory.message_manager = FakeMessageManager()
    memory.message_manager.messages = oldest_first

    pages = [
        await memory.get_user_mem_by_page(
            user_id="u1",
            scope_id="s1",
            page_size=3,
            page_idx=page_idx,
            memory_type=MemoryType.MIDDLE_TERM_MEMORY,
        )
        for page_idx in range(1, 5)
    ]

    assert [[item.mem_id for item in page] for page in pages] == [
        ["middle_000", "middle_001", "middle_002"],
        ["middle_003", "middle_004", "middle_005"],
        ["middle_006", "middle_007", "middle_008"],
        ["middle_009"],
    ]
    all_ids = [item.mem_id for page in pages for item in page]
    assert len(all_ids) == len(set(all_ids)) == 10
    assert [call["offset"] for call in memory.message_manager.page_calls] == [0, 3, 6, 9]
    assert [call["limit"] for call in memory.message_manager.page_calls] == [3, 3, 3, 3]


@pytest.mark.asyncio
async def test_get_user_mem_by_page_uses_fixed_size_batches_for_deep_page():
    newest_first = [
        {
            "id": f"long_{idx:03d}",
            "mem": f"长期记忆 {idx}",
            "mem_type": MemoryType.SEMANTIC_MEMORY.value,
            "timestamp": datetime(2026, 7, 21, 12, idx % 60, tzinfo=timezone.utc),
        }
        for idx in range(250)
    ]
    memory = LongTermMemory()
    memory.search_manager = FakeSearchManager(newest_first)
    memory.message_manager = FakeMessageManager()

    await memory.get_user_mem_by_page(
        user_id="u1",
        scope_id="s1",
        page_size=50,
        page_idx=5,
        memory_type=MemoryType.UNKNOWN,
    )

    assert [call["limit"] for call in memory.search_manager.calls] == [100, 100, 50]
    assert [call["offset"] for call in memory.search_manager.calls] == [0, 100, 200]


@pytest.mark.asyncio
async def test_get_user_mem_by_page_stops_when_backend_makes_no_progress():
    item = {
        "id": "long_001",
        "mem": "长期记忆",
        "mem_type": MemoryType.SEMANTIC_MEMORY.value,
        "timestamp": datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
    }

    class RepeatingSearchManager(FakeSearchManager):
        async def list_user_mem_by_offset(self, user_id, scope_id, offset, limit, mem_type=None, *, filters=None):
            self.calls.append({"offset": offset, "limit": limit})
            return [item] * limit

    memory = LongTermMemory()
    memory.search_manager = RepeatingSearchManager([])
    memory.message_manager = FakeMessageManager()

    results = await memory.get_user_mem_by_page(
        user_id="u1",
        scope_id="s1",
        page_size=100,
        page_idx=2,
        memory_type=MemoryType.UNKNOWN,
    )

    assert results == []
    assert len(memory.search_manager.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("page_size", "page_idx"), [(0, 1), (-1, 1), (10, 0), (10, -1)])
async def test_get_user_mem_by_page_rejects_invalid_pagination(page_size: int, page_idx: int):
    memory = LongTermMemory()
    memory.search_manager = FakeSearchManager([])
    memory.message_manager = FakeMessageManager()

    with pytest.raises(BaseError, match="page_size must be positive"):
        await memory.get_user_mem_by_page(
            user_id="u1",
            scope_id="s1",
            page_size=page_size,
            page_idx=page_idx,
            memory_type=MemoryType.UNKNOWN,
        )
