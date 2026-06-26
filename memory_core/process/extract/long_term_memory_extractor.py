# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import json
from datetime import datetime, timedelta
from typing import Dict, Any
from foundation.llm import JsonOutputParser
from memory_core.process.extract.common import ExtractMemoryParams
from memory_core.prompts.prompt_applier import PromptApplier
from common.logging import memory_logger
from common.logging.events import LogEventType
from memory_core.config.config import MemoryScopeConfig


class LongTermMemoryExtractor:
    def __init__(self) -> None:
        pass

    @staticmethod
    async def extract_long_term_memory(
        extract_memory_paras: ExtractMemoryParams,
        timestamp: str,
        scope_config: MemoryScopeConfig,
        retries: int = 3,
    ) -> Dict[str, Any]:
        if not scope_config:
            scope_config = MemoryScopeConfig()
        # 多主体场景：开启后 assistant 角色说话人的消息也作为抽取目标
        extract_assistant = bool(getattr(scope_config, "extract_assistant_memory", False))

        # 默认（单主体）沿用 name or role 的说话人标识；多主体下用纯 role，使前缀与
        # prompt 的角色锁定规则（user / assistant）逐字一致。
        def _speaker(msg) -> str:
            return msg.role if extract_assistant else (msg.name or msg.role)

        reference_str = ""
        input_msg_str = ""
        for msg in extract_memory_paras.history_messages:
            reference_str += f"{_speaker(msg)}: {msg.content}\n"
        for msg in extract_memory_paras.messages:
            reference_str += f"{_speaker(msg)}: {msg.content}\n"
            if msg.role == "user" or (extract_assistant and msg.role == "assistant"):
                input_msg_str += f"{_speaker(msg)}: {msg.content}\n"

        current_week = LongTermMemoryExtractor._build_time_context(timestamp)
        if extract_assistant:
            subject_locking_rule = (chr(13) + chr(10)).join([
                "  - 本对话含多个真实参与者，【目标消息】每行前缀“角色:”（如 user / assistant）即该句的主体说话人。",
                "  - 以每行前缀的角色标识为该句主体；句中第一人称（我/本人/我们）指代该前缀说话人；每位参与者均为合法主体。",
                "  - 主体自治：每行只提取该行说话人关于【其本人】的事实；该说话人以第二人称对【另一参与者】"
                "所作的属性/身份/状态断言，不得记为该参与者的事实，应整句丢弃（不得据此为对方新增或改写任何记忆）。",
                "  - 若无主语且无法确定主体 -> 直接丢弃整句，绝不提取。",
            ])
            subject_scope = "该句的主体说话人"
            subject_naming_rule = "主语改为该句前缀标识的说话人（取自该句前缀，例如 user / assistant），与前缀保持一致"
            # 多主体·主体自治：变更指令是发令者的专属权限，只能改写发令所在行角色【自己的】记忆；
            # 跨主体的“改为/删除”句（如 assistant 行要改 user 的信息）不得提取为 instruct，
            # 避免一方经 instruct 通道越权改写另一主体的记忆。
            instruct_issuer_rule = (chr(13) + chr(10)).join([
                "       - 【多主体·主体自治】变更记忆指令只能改写【发令所在行的角色】自己的既有记忆：",
                "         · assistant 行里的“改为/删除”句，只能针对 assistant 自己的记忆；"
                "user 行里的，只能针对 user 自己的；",
                "         · 若某条“改为/删除”句的目标记忆属于另一主体"
                "（如 assistant 行要改 user 的信息、或 user 行要改 assistant 的信息）→"
                "【不得】提取为 instruct_memories，按普通陈述或直接丢弃处理。",
            ])
        else:
            subject_locking_rule = (chr(13) + chr(10)).join([
                "  - 若主语是“我”、“本人”、“我们” -> 锁定为用户本人。",
                "  - 若主语是其他情况 -> 锁定为其它主体。",
                "  - 若无主语 -> 直接丢弃整句，绝不提取。",
            ])
            subject_scope = "用户本人"
            subject_naming_rule = '主语全部改为"用户"'
            # 单主体路径 prompt 必须逐字不变：占位符填空串。
            instruct_issuer_rule = ""
        prompt_content = PromptApplier().apply(
            "fragment_memory_prompt",
            {
                "conversation_time": timestamp,
                "input_messages": input_msg_str,
                "reference_messages": reference_str,
                "user_profile_definition": scope_config.user_profile_definition or "",
                "semantic_memory_definition": scope_config.semantic_memory_definition or "",
                "episodic_memory_definition": scope_config.episodic_memory_definition or "",
                "current_week": current_week,
                "subject_locking_rule": subject_locking_rule,
                "subject_scope": subject_scope,
                "subject_naming_rule": subject_naming_rule,
                "instruct_issuer_rule": instruct_issuer_rule,
            },
        )
        model_input = [{"role": "user", "content": prompt_content}]
        parser = JsonOutputParser()
        for attempt in range(retries):
            try:
                response = await extract_memory_paras.base_chat_model.invoke(messages=model_input)
                result = await parser.parse(response.content)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError as e:
                if attempt < retries - 1:
                    continue
                memory_logger.error(
                    "Long term memory extractor model output format error",
                    event_type=LogEventType.MEMORY_PROCESS,
                    exception=str(e)
                )
        return {}

    @staticmethod
    def _build_time_context(timestamp: str) -> str:
        try:
            dt = datetime.fromisoformat(timestamp)
            monday = dt - timedelta(days=dt.weekday())
            sunday = monday + timedelta(days=6)
            return (
                f"{monday.year}年{monday.month}月{monday.day}日(周一)～"
                f"{sunday.year}年{sunday.month}月{sunday.day}日(周日)"
                f"（即{monday.strftime('%m.%d')}～{sunday.strftime('%m.%d')}）"
            )
        except (ValueError, TypeError):
            return timestamp
