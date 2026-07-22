# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""
Step 5 unit tests — `is_important` propagation along the online + dreaming
write path.

Coverage:
  - `FragmentMemoryUnit` / `SummaryUnit` default `is_important=False`
  - `KnowledgeItem` default `is_important=False`, and the dreaming write
    path (`MemoryUnitKnowledgeStore.promote`) forwards the flag onto the
    produced `FragmentMemoryUnit`
  - `MemoryScopeConfig.important_memory_definition` default text
  - `Generator._normalize_memory_entry` accepts both the new dict shape
    `{"content", "is_important"}` and the legacy pure-string shape; coerces
    loose truthy variants (`True`, `1`, `"true"`, `"True"`) strictly,
    rejecting `"yes"` / `"1"` (string) / unknown keys
  - `Generator._get_fragment_memory_unit` end-to-end: LLM payload → units,
    preserving `is_important` and dropping invalid entries
  - `FragmentMemoryManager._convert_to_memory_doc` puts `is_important` onto
    the resulting `MemoryDoc`
  - `SummaryManager._convert_to_memory_docs` puts `is_important` onto the
    resulting `MemoryDoc` and skips empty summaries
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.memory_core.config.config import MemoryScopeConfig
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import (
    FragmentMemoryUnit,
    MemoryType,
    SummaryUnit,
)
from jiuwen_memory.memory_core.manage.mem_model.data_id_manager import DataIdManager
from jiuwen_memory.memory_core.process.dreaming.store import KnowledgeItem, MemoryUnitKnowledgeStore
from jiuwen_memory.memory_core.process.extract.generation import Generator
from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import FragmentMemoryManager
from jiuwen_memory.memory_core.manage.index.summary_manager import SummaryManager


# --------------------------------------------------------------- data class defaults


class TestDataClassDefaults:
    @staticmethod
    def test_fragment_memory_unit_defaults_is_important_false():
        unit = FragmentMemoryUnit(
            mem_type=MemoryType.USER_PROFILE,
            mem_id="m1",
            content="I am a data analyst",
        )
        assert unit.is_important is False

    @staticmethod
    def test_fragment_memory_unit_is_important_can_be_set_true():
        unit = FragmentMemoryUnit(
            mem_type=MemoryType.USER_PROFILE,
            mem_id="m1",
            content="user identity: admin",
            is_important=True,
        )
        assert unit.is_important is True

    @staticmethod
    def test_summary_unit_defaults_is_important_false():
        unit = SummaryUnit(
            mem_id="s1",
            summary="a short summary",
        )
        assert unit.is_important is False
        assert unit.mem_type is MemoryType.SUMMARY

    @staticmethod
    def test_summary_unit_is_important_can_be_set_true():
        unit = SummaryUnit(
            mem_id="s1",
            summary="key decision: user committed to a 12-month plan",
            is_important=True,
        )
        assert unit.is_important is True


# --------------------------------------------------------------- KnowledgeItem / dreaming


class TestKnowledgeItemDefault:
    @staticmethod
    def test_knowledge_item_defaults_is_important_false():
        item = KnowledgeItem(
            mem_type="semantic_memory",
            content="likes python",
            source_session_id="sess_a",
        )
        assert item.is_important is False

    @staticmethod
    def test_knowledge_item_is_important_can_be_set_true():
        item = KnowledgeItem(
            mem_type="semantic_memory",
            content="user is the account owner",
            source_session_id="sess_a",
            is_important=True,
        )
        assert item.is_important is True


@pytest.mark.asyncio
async def test_promote_forwards_is_important_to_fragment_unit():
    """
    MemoryUnitKnowledgeStore.promote must propagate KnowledgeItem.is_important
    onto the generated FragmentMemoryUnit.
    """
    write_manager = MagicMock()
    write_manager.add_memories = AsyncMock(
        side_effect=lambda u, s, mems, llm: [m for lst in mems.values() for m in lst]
    )

    backing: dict = {}

    async def _exclusive_set(key, value, expiry=None):
        if key in backing:
            return False
        backing[key] = value
        return True

    async def _get(key):
        return backing.get(key)

    async def _delete(key):
        backing.pop(key, None)

    kv_store = MagicMock()
    kv_store.exclusive_set = AsyncMock(side_effect=_exclusive_set)
    kv_store.get = AsyncMock(side_effect=_get)
    kv_store.delete = AsyncMock(side_effect=_delete)

    store = MemoryUnitKnowledgeStore(
        write_manager, kv_store, llm="LLM",
        user_id="u", scope_id="s",
    )

    await store.promote([
        KnowledgeItem(
            mem_type="semantic_memory",
            content="user identity: admin",
            source_session_id="sess_a",
            is_important=True,
        ),
        KnowledgeItem(
            mem_type="semantic_memory",
            content="likes python",
            source_session_id="sess_a",
            is_important=False,
        ),
    ])

    args, _ = write_manager.add_memories.call_args
    _, _, memories, _ = args
    units = memories["semantic_memory"]
    assert len(units) == 2
    important_flags = {u.content: u.is_important for u in units}
    assert important_flags["user identity: admin"] is True
    assert important_flags["likes python"] is False


# --------------------------------------------------------------- MemoryScopeConfig


class TestImportantMemoryDefinition:
    @staticmethod
    def test_default_definition_is_nonempty_string():
        cfg = MemoryScopeConfig()
        assert isinstance(cfg.important_memory_definition, str)
        assert cfg.important_memory_definition.strip() != ""

    @staticmethod
    def test_custom_definition_round_trips():
        cfg = MemoryScopeConfig(important_memory_definition="用户身份、长期承诺")
        assert cfg.important_memory_definition == "用户身份、长期承诺"


# --------------------------------------------------------------- _normalize_memory_entry


class TestNormalizeMemoryEntry:
    @staticmethod
    def test_str_entry_returns_false_flag():
        content, flag = Generator._normalize_memory_entry("a plain string")  # pylint: disable=protected-access
        assert content == "a plain string"
        assert flag is False

    @staticmethod
    def test_dict_with_content_and_is_important_true():
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"content": "user is admin", "is_important": True}
        )
        assert content == "user is admin"
        assert flag is True

    @staticmethod
    def test_dict_with_content_and_is_important_false():
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"content": "likes python", "is_important": False}
        )
        assert content == "likes python"
        assert flag is False

    @staticmethod
    def test_dict_missing_is_important_defaults_false():
        content, flag = Generator._normalize_memory_entry({"content": "no flag"})  # pylint: disable=protected-access
        assert content == "no flag"
        assert flag is False

    @staticmethod
    def test_dict_with_mem_content_key_legacy():
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"mem_content": "legacy shape", "is_important": True}
        )
        assert content == "legacy shape"
        assert flag is True

    @staticmethod
    def test_int_one_is_coerced_as_true():
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"content": "x", "is_important": 1}
        )
        assert flag is True

    @staticmethod
    def test_string_true_is_coerced_as_true():
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"content": "x", "is_important": "true"}
        )
        assert flag is True

    @staticmethod
    def test_string_true_uppercase_is_coerced_as_true():
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"content": "x", "is_important": "True"}
        )
        assert flag is True

    @staticmethod
    def test_string_yes_is_rejected():
        # only `True`, int `1`, or string `true`/`True` are accepted — "yes" must
        # NOT silently flip a memory into protected status
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"content": "x", "is_important": "yes"}
        )
        assert flag is False

    @staticmethod
    def test_string_one_is_rejected():
        # string "1" is NOT the same as int 1 — reject to avoid accidental protection
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"content": "x", "is_important": "1"}
        )
        assert flag is False

    @staticmethod
    def test_dict_without_content_key_falls_back_to_longest_str():
        # An unrecognized dict shape: take the longest string-ish value
        # not flagged as meta (type / mem_type / is_important).
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"type": "semantic_memory", "note": "hello world", "x": "hi"}
        )
        assert content == "hello world"
        assert flag is False

    @staticmethod
    def test_dict_with_only_meta_keys_returns_none_content():
        content, flag = Generator._normalize_memory_entry(  # pylint: disable=protected-access
            {"type": "x", "mem_type": "y", "is_important": True}
        )
        assert content is None
        # even if is_important was True, without content there is nothing to protect
        assert flag is True

    @staticmethod
    def test_none_entry_returns_none_and_false():
        content, flag = Generator._normalize_memory_entry(None)  # pylint: disable=protected-access
        assert content is None
        assert flag is False

    @staticmethod
    def test_int_entry_returns_none_and_false():
        content, flag = Generator._normalize_memory_entry(42)  # pylint: disable=protected-access
        assert content is None
        assert flag is False


# --------------------------------------------------------------- _get_fragment_memory_unit


@pytest.mark.asyncio
async def test_get_fragment_memory_unit_preserves_is_important_from_llm_payload():
    """
    End-to-end: `_get_fragment_memory_unit` must parse the new dict shape and
    carry `is_important` onto each `FragmentMemoryUnit`.
    """
    data_id_generator = MagicMock(spec=DataIdManager)
    data_id_generator.generate_next_id = AsyncMock(side_effect=[101, 102, 103])
    generator = Generator(data_id_generator=data_id_generator)

    memory_dict = {
        "user_profile": [
            {"content": "user identity: admin", "is_important": True},
            {"content": "likes python", "is_important": False},
        ],
        "semantic_memory": [
            {"content": "a key fact", "is_important": True},
        ],
        # unsupported category — must be skipped
        "unknown_category": [
            {"content": "dropped", "is_important": True},
        ],
    }

    units = await generator._get_fragment_memory_unit(  # pylint: disable=protected-access
        user_id="u",
        message_mem_id="msg-1",
        memory_dict=memory_dict,
        timestamp="2026-07-21 10:00:00",
    )

    # 2 user_profile + 1 semantic_memory + 0 unknown = 3
    assert len(units) == 3
    by_content = {u.content: u for u in units}
    assert by_content["user identity: admin"].is_important is True
    assert by_content["likes python"].is_important is False
    assert by_content["a key fact"].is_important is True
    # mem_type preserved as enum
    assert by_content["user identity: admin"].mem_type is MemoryType.USER_PROFILE
    assert by_content["a key fact"].mem_type is MemoryType.SEMANTIC_MEMORY


@pytest.mark.asyncio
async def test_get_fragment_memory_unit_accepts_legacy_string_shape():
    """Old LLM output as list[str] must still work (is_important defaults False)."""
    data_id_generator = MagicMock(spec=DataIdManager)
    data_id_generator.generate_next_id = AsyncMock(return_value=201)
    generator = Generator(data_id_generator=data_id_generator)

    memory_dict = {
        "semantic_memory": ["legacy plain string"],
    }
    units = await generator._get_fragment_memory_unit(  # pylint: disable=protected-access
        user_id="u",
        message_mem_id="msg-2",
        memory_dict=memory_dict,
        timestamp="2026-07-21 10:00:00",
    )
    assert len(units) == 1
    assert units[0].content == "legacy plain string"
    assert units[0].is_important is False


@pytest.mark.asyncio
async def test_get_fragment_memory_unit_drops_empty_content():
    data_id_generator = MagicMock(spec=DataIdManager)
    data_id_generator.generate_next_id = AsyncMock(return_value=301)
    generator = Generator(data_id_generator=data_id_generator)

    memory_dict = {
        "semantic_memory": [
            {"content": "   ", "is_important": True},   # whitespace-only
            {"content": "", "is_important": True},       # empty
            {"is_important": True},                      # no content key, no fallback
        ],
    }
    units = await generator._get_fragment_memory_unit(  # pylint: disable=protected-access
        user_id="u",
        message_mem_id="msg-3",
        memory_dict=memory_dict,
        timestamp="2026-07-21 10:00:00",
    )
    assert units == []


# --------------------------------------------------------------- FragmentMemoryManager._convert_to_memory_doc


class TestFragmentConvertToMemoryDoc:
    @staticmethod
    def test_is_important_propagates_to_memory_doc():
        manager = FragmentMemoryManager(memory_index=MagicMock())

        unit = FragmentMemoryUnit(
            mem_type=MemoryType.USER_PROFILE,
            mem_id="m1",
            content="user identity: admin",
            is_important=True,
        )
        doc = manager._convert_to_memory_doc(unit)  # pylint: disable=protected-access
        assert isinstance(doc, MemoryDoc)
        assert doc.is_important is True
        assert doc.text == "user identity: admin"
        assert doc.type == MemoryType.USER_PROFILE.value

    @staticmethod
    def test_default_is_important_false_propagates():
        manager = FragmentMemoryManager(memory_index=MagicMock())
        unit = FragmentMemoryUnit(
            mem_type=MemoryType.SEMANTIC_MEMORY,
            mem_id="m2",
            content="likes python",
        )
        doc = manager._convert_to_memory_doc(unit)  # pylint: disable=protected-access
        assert doc.is_important is False


# --------------------------------------------------------------- SummaryManager._convert_to_memory_docs


class TestSummaryConvertToMemoryDocs:
    @staticmethod
    def _make_manager():
        return SummaryManager(memory_index=MagicMock())

    @staticmethod
    def test_is_important_propagates_to_memory_doc():
        from jiuwen_memory.memory_core.manage.mem_model.memory_unit import SummaryUnit as _SummaryUnit
        unit = _SummaryUnit(
            mem_id="s1",
            summary="user committed to 12-month plan",
            is_important=True,
        )
        docs = TestSummaryConvertToMemoryDocs._make_manager()._convert_to_memory_docs({"summary": [unit]})  # pylint: disable=protected-access
        assert len(docs) == 1
        assert docs[0].is_important is True
        assert docs[0].text == "user committed to 12-month plan"

    @staticmethod
    def test_empty_summary_is_skipped():
        from jiuwen_memory.memory_core.manage.mem_model.memory_unit import SummaryUnit as _SummaryUnit
        unit = _SummaryUnit(mem_id="s2", summary="   ", is_important=True)
        docs = TestSummaryConvertToMemoryDocs._make_manager()._convert_to_memory_docs({"summary": [unit]})  # pylint: disable=protected-access
        assert docs == []

    @staticmethod
    def test_only_summary_mem_type_passes_through():
        # _convert_to_memory_docs filters by self.mem_type == "summary";
        # a dict with a different key should produce no docs.
        from jiuwen_memory.memory_core.manage.mem_model.memory_unit import SummaryUnit as _SummaryUnit
        unit = _SummaryUnit(mem_id="s3", summary="hello", is_important=False)
        docs = TestSummaryConvertToMemoryDocs._make_manager()._convert_to_memory_docs({"semantic_memory": [unit]})  # pylint: disable=protected-access
        assert docs == []


# --------------------------------------------------------------- update() field preservation


@pytest.mark.asyncio
async def test_fragment_update_preserves_is_important_and_blacklisted():
    """
    FragmentMemoryManager.update must preserve `is_important` and
    `blacklisted` from the existing doc: update_mem_by_id
    only rewrites content; Ebbinghaus flags must survive the rewrite.
    """
    from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import (
        FragmentMemoryManager as _FragmentMemoryManager,
    )

    memory_index = MagicMock()
    memory_index.get_by_id = AsyncMock(return_value=MemoryDoc(
        id="m1",
        text="old content",
        type=MemoryType.USER_PROFILE.value,
        timestamp=datetime.now(timezone.utc).astimezone(),
        fields={"source_id": "msg-1"},
        is_important=True,
        blacklisted=True,
    ))
    memory_index.update_memories = AsyncMock()

    manager = _FragmentMemoryManager(memory_index=memory_index)
    ok = await manager.update("u", "s", "m1", "new content")
    assert ok is True
    memory_index.update_memories.assert_awaited_once()
    # update_memories(user_id, scope_id, [updated_doc]) — args[2] is the list
    updated_doc = memory_index.update_memories.call_args.args[2][0]
    assert updated_doc.text == "new content"
    assert updated_doc.is_important is True
    assert updated_doc.blacklisted is True


@pytest.mark.asyncio
async def test_summary_update_preserves_is_important_and_blacklisted():
    """
    SummaryManager.update must preserve `is_important` and `blacklisted`
    from the existing doc.
    """
    memory_index = MagicMock()
    memory_index.get_by_id = AsyncMock(return_value=MemoryDoc(
        id="s1",
        text="old summary",
        type=MemoryType.SUMMARY.value,
        timestamp=datetime.now(timezone.utc).astimezone(),
        fields={"source_id": "msg-1", "metadata": {}},
        is_important=True,
        blacklisted=True,
    ))
    memory_index.update_memories = AsyncMock()

    manager = SummaryManager(memory_index=memory_index)
    ok = await manager.update("u", "s", "s1", "new summary")
    assert ok is True
    updated_doc = memory_index.update_memories.call_args.args[2][0]
    assert updated_doc.text == "new summary"
    assert updated_doc.is_important is True
    assert updated_doc.blacklisted is True
