# coding: utf-8
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.foundation.llm.schema.message import AssistantMessage, BaseMessage, UserMessage
from jiuwen_memory.memory_core.config.config import AgentMemoryConfig, MemoryEngineConfig
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
from jiuwen_memory.memory_core.process.extract.common import ExtractMemoryParams
from jiuwen_memory.memory_core.process.extract.long_term_memory_extractor import LongTermMemoryExtractor


@pytest.fixture(autouse=True)
def reset_long_term_memory_singleton():
    instance = Singleton._instances.pop(LongTermMemory, None)
    if instance and instance._executor_active:
        instance._batch_executor.shutdown(wait=False)

    yield

    instance = Singleton._instances.pop(LongTermMemory, None)
    if instance and instance._executor_active:
        instance._batch_executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_process_dialogue_batch_converts_messages_by_role_not_content():
    memory = LongTermMemory()
    memory._sys_mem_config = MemoryEngineConfig()
    memory._get_scope_llm = AsyncMock(return_value=MagicMock())
    memory._create_semantic_store_with_embedding = AsyncMock(return_value=MagicMock())
    memory._get_scope_config = AsyncMock(return_value=MagicMock(extract_assistant_memory=False))
    memory.generator = MagicMock()
    memory.generator.gen_all_memory = AsyncMock(return_value=[MagicMock()])
    memory.write_manager = MagicMock()
    memory.write_manager.add_memories = AsyncMock()

    dialogue_batch = [
        BaseMessage(role="assistant", content="User：this is still an assistant reply"),
        BaseMessage(role="user", content="Assistant：this is still a user request"),
    ]

    result = await memory._process_dialogue_batch_to_long_term_safe(
        dialogue_batch=dialogue_batch,
        agent_config=AgentMemoryConfig(),
        user_id="user1",
        scope_id="scope1",
        session_id="session1",
        timestamp_str="2025-01-01T00:00:00Z",
        mem_ids=["middle-1.mem"],
    )

    assert result["success"] is True
    converted_messages = memory.generator.gen_all_memory.await_args.kwargs["messages"]
    assert isinstance(converted_messages[0], AssistantMessage)
    assert converted_messages[0].role == "assistant"
    assert converted_messages[0].content == dialogue_batch[0].content
    assert isinstance(converted_messages[1], UserMessage)
    assert converted_messages[1].role == "user"
    assert converted_messages[1].content == dialogue_batch[1].content


@pytest.mark.asyncio
async def test_middle_mem_to_long_keeps_dialogue_object_and_skips_initial_continuity_check():
    memory = LongTermMemory()
    dialogue = BaseMessage(role="user", content="first dialogue")
    memory.message_manager = MagicMock()
    memory.message_manager.get = AsyncMock(return_value=[(dialogue, "ts1", "mem1")])
    memory.check_dialogue_continuity = AsyncMock(return_value="false")
    memory._batch_delete_middle_memories = AsyncMock()
    memory._batch_delete_middle_messages = AsyncMock()
    captured_batches = []

    def fake_run_batch_in_thread(batch_data, *args):
        captured_batches.append(batch_data)
        return {"success": True, "mem_ids": batch_data["mem_ids"], "batch_size": len(batch_data["dialogues"])}

    memory._run_batch_in_thread = fake_run_batch_in_thread

    await memory.middle_mem_to_long(
        agent_config=AgentMemoryConfig(),
        user_id="user1",
        scope_id="scope1",
        session_id="session1",
    )

    memory.check_dialogue_continuity.assert_not_awaited()
    assert captured_batches[0]["dialogues"] == [dialogue]
    assert isinstance(captured_batches[0]["dialogues"][0], BaseMessage)


@pytest.mark.asyncio
async def test_middle_mem_to_long_passes_dialogue_content_to_continuity_check():
    memory = LongTermMemory()
    previous_dialogue = BaseMessage(role="user", content="previous dialogue")
    current_dialogue = BaseMessage(role="assistant", content="current dialogue")
    memory.message_manager = MagicMock()
    memory.message_manager.get = AsyncMock(
        return_value=[
            (previous_dialogue, "ts1", "mem1"),
            (current_dialogue, "ts2", "mem2"),
        ]
    )
    memory.check_dialogue_continuity = AsyncMock(return_value="true")
    memory._batch_delete_middle_memories = AsyncMock()
    memory._batch_delete_middle_messages = AsyncMock()
    captured_batches = []

    def fake_run_batch_in_thread(batch_data, *args):
        captured_batches.append(batch_data)
        return {"success": True, "mem_ids": batch_data["mem_ids"], "batch_size": len(batch_data["dialogues"])}

    memory._run_batch_in_thread = fake_run_batch_in_thread

    await memory.middle_mem_to_long(
        agent_config=AgentMemoryConfig(),
        user_id="user1",
        scope_id="scope1",
        session_id="session1",
    )

    memory.check_dialogue_continuity.assert_awaited_once_with(
        scope_id="scope1",
        previous_dialogue="previous dialogue",
        current_dialogue="current dialogue",
    )
    assert captured_batches[0]["dialogues"] == [previous_dialogue, current_dialogue]


@pytest.mark.asyncio
async def test_extractor_targets_user_and_assistant_messages_when_enabled(monkeypatch):
    captured_prompt_variables = {}

    def fake_apply(self, prompt_name, variables):
        captured_prompt_variables.update(variables)
        return "{}"

    monkeypatch.setattr(
        "jiuwen_memory.memory_core.process.extract.long_term_memory_extractor.PromptApplier.apply",
        fake_apply,
    )
    llm = MagicMock()
    llm.invoke = AsyncMock(return_value=AssistantMessage(content="{}"))
    extract_params = ExtractMemoryParams(
        user_id="user1",
        scope_id="scope1",
        messages=[
            BaseMessage(role="user", content="I like coffee"),
            BaseMessage(role="assistant", content="I like tea"),
        ],
        history_messages=[],
        base_chat_model=llm,
    )
    scope_config = MagicMock(extract_assistant_memory=True)
    scope_config.user_profile_definition = ""
    scope_config.semantic_memory_definition = ""
    scope_config.episodic_memory_definition = ""

    await LongTermMemoryExtractor.extract_long_term_memory(
        extract_memory_paras=extract_params,
        timestamp="2025-01-01T00:00:00",
        scope_config=scope_config,
    )

    assert "user: I like coffee" in captured_prompt_variables["input_messages"]
    assert "assistant: I like tea" in captured_prompt_variables["input_messages"]
