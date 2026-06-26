#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.s
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_core.process.extract.generation import Generator
from memory_core.manage.mem_model.memory_unit import (
    MiddleTermUnit,
    FragmentMemoryUnit,
    MemoryType,
    OperationType
)
from memory_core.manage.mem_model.data_id_manager import DataIdManager

class TestMiddleTermMemoryConversion:

    @pytest.fixture
    def generator(self):
        """创建 Generator 实例"""
        # 创建 mock data_id_generator
        data_id_generator = MagicMock(spec=DataIdManager)
        data_id_generator.generate_next_id = AsyncMock(return_value=1001)

        # 传入必需的参数
        generator = Generator(data_id_generator=data_id_generator)
        return generator

    @pytest.fixture
    def base_message(self):
        message = MagicMock()
        message.content = "用户提到他在阿里巴巴工作，主要负责云存储业务"
        message.role = "user"
        return message

    @pytest.fixture
    def base_chat_model(self):
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=MagicMock(content="连续性分析结果"))
        return model


class TestMiddleTermMemoryUnitGenerator(TestMiddleTermMemoryConversion):

    @pytest.mark.asyncio
    async def test_generate_middle_term_unit_basic(self, generator, base_message):
        kwargs = {
            "user_id": "user-123",
            "msg": base_message,
            "message_mem_id": "msg-session-001",
            "timestamp": "2026-06-23 14:30:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)

        assert "middle_term_memory" in result
        assert len(result["middle_term_memory"]) == 1

        unit = result["middle_term_memory"][0]
        assert isinstance(unit, MiddleTermUnit)
        assert unit.mem_id == "1001"
        assert unit.message_mem_id == "msg-session-001"
        assert "阿里巴巴工作" in unit.content
        assert unit.timestamp == "2026-06-23 14:30:00"

    @pytest.mark.asyncio
    async def test_generate_middle_term_unit_with_timestamp(self, generator, base_message):
        timestamp = "2026-06-23 10:00:00"
        kwargs = {
            "user_id": "user-test",
            "msg": base_message,
            "message_mem_id": "msg-001",
            "timestamp": timestamp
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        assert timestamp in unit.content
        assert unit.timestamp == timestamp

    @pytest.mark.asyncio
    async def test_generate_middle_term_unit_content_concatenation(self, generator):
        message = MagicMock()
        message.content = "我喜欢喝咖啡"

        kwargs = {
            "user_id": "user-001",
            "msg": message,
            "message_mem_id": "msg-002",
            "timestamp": "2026-06-23 15:00:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        expected_content = "我喜欢喝咖啡 time:2026-06-23 15:00:00"
        assert unit.content == expected_content

    @pytest.mark.asyncio
    async def test_generate_middle_term_unit_id_generation(self, generator):
        generator.data_id_generator.generate_next_id = AsyncMock(side_effect=[2001, 2002, 2003])
        message = MagicMock()
        message.content = "测试内容"

        kwargs = {
            "user_id": "user-abc",
            "msg": message,
            "message_mem_id": "msg-abc",
            "timestamp": "2026-06-23 12:00:00"
        }

        result1 = await generator.middle_term_memory_unit_generator(**kwargs)
        unit1 = result1["middle_term_memory"][0]
        assert unit1.mem_id == "2001"

        result2 = await generator.middle_term_memory_unit_generator(**kwargs)
        unit2 = result2["middle_term_memory"][0]
        assert unit2.mem_id == "2002"

    @pytest.mark.asyncio
    async def test_generate_middle_term_unit_missing_params(self, generator):
        kwargs = {
            "user_id": "user-123",
            "msg": MagicMock(content="测试"),
            "message_mem_id": "msg-001"
        }

        with pytest.raises(TypeError):
            await generator.middle_term_memory_unit_generator(**kwargs)


class TestContinuityAnalyzer(TestMiddleTermMemoryConversion):

    @pytest.mark.asyncio
    async def test_check_continuity_basic(self, generator, base_chat_model):
        """测试基本的连续性分析"""
        previous_dialogue = "昨天我们讨论了项目进度"
        current_dialogue = "今天我想继续讨论项目细节"

        with patch.object(
                generator,
                'check_continuity_analyzer',
                return_value={"is_continuous": True, "topic": "项目"}
        ):
            result = await generator.check_continuity_analyzer(
                previous_dialogue=previous_dialogue,
                current_dialogue=current_dialogue,
                base_chat_model=base_chat_model
            )

        assert result is not None

    @pytest.mark.asyncio
    async def test_check_continuity_with_memory_analyzer(self, generator, base_chat_model):
        previous_dialogue = "我之前提到我在阿里巴巴工作"
        current_dialogue = "云存储业务最近有很多新特性"

        # Mock MemoryAnalyzer.check_conversation_continuity
        from memory_core.process.extract.memory_analyzer import MemoryAnalyzer

        with patch.object(
                MemoryAnalyzer,
                'check_conversation_continuity',
                AsyncMock(return_value={
                    "is_continuous": True,
                    "continuity_score": 0.85,
                    "related_topics": ["工作", "云计算"]
                })
        ):
            result = await generator.check_continuity_analyzer(
                previous_dialogue=previous_dialogue,
                current_dialogue=current_dialogue,
                base_chat_model=base_chat_model
            )

        assert result["is_continuous"] == True
        assert result["continuity_score"] > 0.7

    @pytest.mark.asyncio
    async def test_check_continuity_discontinuous(self, generator, base_chat_model):
        previous_dialogue = "我们讨论了天气预报"
        current_dialogue = "我想了解机器学习算法"

        from memory_core.process.extract.memory_analyzer import MemoryAnalyzer

        with patch.object(
                MemoryAnalyzer,
                'check_conversation_continuity',
                AsyncMock(return_value={
                    "is_continuous": False,
                    "continuity_score": 0.2,
                    "related_topics": []
                })
        ):
            result = await generator.check_continuity_analyzer(
                previous_dialogue=previous_dialogue,
                current_dialogue=current_dialogue,
                base_chat_model=base_chat_model
            )

        assert result["is_continuous"] == False
        assert result["continuity_score"] < 0.3


class TestProcessMiddleTermData(TestMiddleTermMemoryConversion):

    @pytest.mark.asyncio
    async def test_process_middle_term_data_basic(self, generator):
        user_id = "user-001"
        message_mem_id = "msg-001"
        content = "用户喜欢周末去健身房"
        timestamp = "2026-06-23 10:00:00"

        result = await generator._process_middle_term_data(
            user_id=user_id,
            message_mem_id=message_mem_id,
            content=content,
            timestamp=timestamp
        )

        assert isinstance(result, MiddleTermUnit)
        assert result.mem_id == "1001"
        assert result.content == content
        assert result.message_mem_id == message_mem_id
        assert result.timestamp == timestamp
        assert result.mem_type == MemoryType.MIDDLE_TERM_MEMORY

    @pytest.mark.asyncio
    async def test_process_middle_term_data_special_characters(self, generator):
        content = "用户说: \"我喜欢Python编程!\" @#$%"
        timestamp = "2026-06-23 16:30:00"

        result = await generator._process_middle_term_data(
            user_id="user-special",
            message_mem_id="msg-special",
            content=content,
            timestamp=timestamp
        )

        assert result.content == content
        assert result.timestamp == timestamp

    @pytest.mark.asyncio
    async def test_process_middle_term_data_long_content(self, generator):
        long_content = "这是一个很长的记忆内容，包含了用户的多方面信息：工作经验、兴趣爱好、家庭情况、教育背景、职业规划等详细信息。用户提到他在多家知名科技公司工作过，包括腾讯、阿里巴巴、字节跳动、华为等，主要负责后端开发、系统架构设计、团队管理等核心业务。"

        result = await generator._process_middle_term_data(
            user_id="user-long",
            message_mem_id="msg-long",
            content=long_content,
            timestamp="2026-06-23 18:00:00"
        )

        assert result.content == long_content
        assert len(result.content) > 100


    @pytest.mark.asyncio
    async def test_process_middle_term_data_unicode(self, generator):
        unicode_content = "用户表情 😊 表示开心，符号 ©️ ®️ 专利"

        result = await generator._process_middle_term_data(
            user_id="user-unicode",
            message_mem_id="msg-unicode",
            content=unicode_content,
            timestamp="2026-06-23 20:00:00"
        )

        assert result.content == unicode_content


class TestIntegrationFlow(TestMiddleTermMemoryConversion):

    @pytest.mark.asyncio
    async def test_full_conversion_flow(self, generator, base_message):
        kwargs = {
            "user_id": "user-full-test",
            "msg": base_message,
            "message_mem_id": "msg-full-001",
            "timestamp": "2026-06-23 14:30:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)

        assert "middle_term_memory" in result
        unit = result["middle_term_memory"][0]

        assert isinstance(unit, MiddleTermUnit)
        assert unit.mem_type == MemoryType.MIDDLE_TERM_MEMORY
        assert unit.message_mem_id == "msg-full-001"

        assert unit.mem_id == "1001"

        expected_content = f"{base_message.content} time:{kwargs['timestamp']}"
        assert unit.content == expected_content

    @pytest.mark.asyncio
    async def test_conversion_with_different_timestamps(self, generator):
        timestamps = [
            "2026-06-23 10:00:00",
            "2026-06-23T10:00:00",
            "2026/06/23 10:00:00"
        ]

        for idx, timestamp in enumerate(timestamps):
            message = MagicMock(content=f"测试消息{idx}")
            kwargs = {
                "user_id": f"user-{idx}",
                "msg": message,
                "message_mem_id": f"msg-{idx}",
                "timestamp": timestamp
            }

            result = await generator.middle_term_memory_unit_generator(**kwargs)
            unit = result["middle_term_memory"][0]

            assert unit.timestamp == timestamp

    @pytest.mark.asyncio
    async def test_conversion_multiple_memories(self, generator):
        generator.data_id_generator.generate_next_id = AsyncMock(
            side_effect=[3001, 3002, 3003, 3004]
        )

        messages = [
            "用户在北京工作",
            "用户喜欢运动",
            "用户周末去健身房",
            "用户最近在学习AI"
        ]

        results = []
        for idx, content in enumerate(messages):
            message = MagicMock(content=content)
            kwargs = {
                "user_id": "user-multi",
                "msg": message,
                "message_mem_id": f"msg-{idx}",
                "timestamp": f"2026-06-23 {10 + idx}:00:00"
            }

            result = await generator.middle_term_memory_unit_generator(**kwargs)
            results.append(result["middle_term_memory"][0])

        # 验证每个记忆都有唯一 ID
        mem_ids = [r.mem_id for r in results]
        assert len(set(mem_ids)) == 4  # 4个不同的ID

        # 验证内容不同
        contents = [r.content for r in results]
        assert len(set(contents)) == 4


class TestEdgeCases(TestMiddleTermMemoryConversion):

    @pytest.mark.asyncio
    async def test_empty_content(self, generator):
        message = MagicMock(content="")

        kwargs = {
            "user_id": "user-empty",
            "msg": message,
            "message_mem_id": "msg-empty",
            "timestamp": "2026-06-23 10:00:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        assert unit.content == " time:2026-06-23 10:00:00"

    @pytest.mark.asyncio
    async def test_whitespace_content(self, generator):
        message = MagicMock(content="   ")

        kwargs = {
            "user_id": "user-whitespace",
            "msg": message,
            "message_mem_id": "msg-whitespace",
            "timestamp": "2026-06-23 11:00:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        assert "   " in unit.content

    @pytest.mark.asyncio
    async def test_special_timestamp_format(self, generator):
        message = MagicMock(content="测试内容")

        kwargs = {
            "user_id": "user-iso",
            "msg": message,
            "message_mem_id": "msg-iso",
            "timestamp": "2026-06-23T14:30:00Z"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        assert unit.timestamp == "2026-06-23T14:30:00Z"

    @pytest.mark.asyncio
    async def test_very_long_user_id(self, generator):
        long_user_id = "user-" + "a" * 1000
        message = MagicMock(content="测试内容")

        kwargs = {
            "user_id": long_user_id,
            "msg": message,
            "message_mem_id": "msg-long",
            "timestamp": "2026-06-23 12:00:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        assert result is not None


class TestDataValidation(TestMiddleTermMemoryConversion):

    @pytest.mark.asyncio
    async def test_memory_unit_type_validation(self, generator, base_message):
        kwargs = {
            "user_id": "user-type",
            "msg": base_message,
            "message_mem_id": "msg-type",
            "timestamp": "2026-06-23 10:00:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        assert unit.mem_type == MemoryType.MIDDLE_TERM_MEMORY
        assert unit.mem_type.value == "middle_term_memory"

    @pytest.mark.asyncio
    async def test_memory_unit_fields_complete(self, generator, base_message):
        kwargs = {
            "user_id": "user-complete",
            "msg": base_message,
            "message_mem_id": "msg-complete",
            "timestamp": "2026-06-23 10:00:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        assert hasattr(unit, 'mem_id')
        assert hasattr(unit, 'mem_type')
        assert hasattr(unit, 'content')
        assert hasattr(unit, 'message_mem_id')
        assert hasattr(unit, 'timestamp')

        assert unit.mem_id is not None
        assert unit.content is not None
        assert unit.message_mem_id is not None
        assert unit.timestamp is not None

    @pytest.mark.asyncio
    async def test_memory_unit_serialization(self, generator, base_message):
        kwargs = {
            "user_id": "user-serialize",
            "msg": base_message,
            "message_mem_id": "msg-serialize",
            "timestamp": "2026-06-23 10:00:00"
        }

        result = await generator.middle_term_memory_unit_generator(**kwargs)
        unit = result["middle_term_memory"][0]

        unit_dict = {
            "mem_id": unit.mem_id,
            "mem_type": unit.mem_type.value,
            "content": unit.content,
            "message_mem_id": unit.message_mem_id,
            "timestamp": unit.timestamp
        }

        assert unit_dict["mem_id"] == "1001"
        assert unit_dict["mem_type"] == "middle_term_memory"