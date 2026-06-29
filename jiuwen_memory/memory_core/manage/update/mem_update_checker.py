# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""
Memory checker module for detecting redundancy and conflicts between memories.

This module provides MemChecker class that uses LLM to analyze whether new
memories are redundant, conflicting, or can coexist with existing memories.
"""

import json
from typing import Dict, List, Tuple
from enum import Enum

from pydantic import BaseModel, Field

from jiuwen_memory.foundation.llm import Model, JsonOutputParser
from jiuwen_memory.memory_core.prompts.prompt_applier import PromptApplier
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType


# Injected into memory_update_check prompt only when the batch may contain
# multiple distinct subjects (extract_assistant_memory). Hardens the soft
# "仅当主体完全匹配时" rule so identical predicates across different subjects
# are not collapsed into one (which would silently drop a subject's memory).
_SUBJECT_EMPHASIS = """
# 主体优先匹配（最高优先级，先于冗余/冲突判断）
- 本场景可能存在多个不同主体的信息。判定任意两条信息前，**必须先各自确定其主体说话人**。
- 铁律：**只要两条信息的主体不同，一律判 `none`（共存）**——即便谓词、属性或取值完全相同，也严禁判 `redundant` 或 `conflicting`。
- 只有在**两条信息的主体完全相同**时，才继续按下列定义判断冗余或冲突。
"""


class CheckResult(str, Enum):
    """Result of memory check operation."""

    REDUNDANT = "redundant"
    CONFLICTING = "conflicting"
    NONE = "none"


class MemoryStatus(str, Enum):
    """Status of memory action."""

    ADD = "add"
    DELETE = "delete"


class MemoryActionItem(BaseModel):
    """
    Represents a memory with its action status.

    Attributes
    ----------
    id : str
        The ID of the memory.
    content : str
        The content of the memory.
    status : MemoryStatus
        The action status (add/delete).
    """

    id: str = Field(..., description="Memory ID")
    content: str = Field(..., description="Memory content")
    status: MemoryStatus = Field(..., description="Action status (add/delete)")


class MemCheckItem(BaseModel):
    """
    Represents a single memory check result item.

    Attributes
    ----------
    info_id : str
        The ID of the new memory being checked.
    info_text : str
        The content of the new memory.
    result : CheckResult
        The check result (redundant/conflicting/none).
    related_infos : Dict[str, str]
        Related old memories that caused redundancy or conflict, format: {id: content}.
    """

    info_id: str = Field(..., description="Memory ID being checked")
    info_text: str = Field(..., description="Content of the memory being checked")
    result: CheckResult = Field(..., description="Check result (redundant/conflicting/none)")
    related_infos: Dict[str, str] = Field(default_factory=dict,
                                          description="Related old memories that caused redundancy or conflict")


def _format_input(new_memories: Dict[str, str], old_memories: Dict[str, str]) -> Tuple[str, str]:
    """
    Format memory dictionaries into strings for prompt input.

    Args:
        new_memories: Dictionary of new memories {id: content}
        old_memories: Dictionary of old memories {id: content}

    Returns:
        Tuple[str, str]: Formatted new and old memory strings
    """
    # Format new memories
    new_info_lines = []
    for mem_id, content in new_memories.items():
        new_info_lines.append(f"{mem_id}: {content}")
    new_info_lines = new_info_lines[::-1]
    new_info_str = "\n".join(new_info_lines)

    # Format old memories
    old_info_lines = []
    for mem_id, content in old_memories.items():
        old_info_lines.append(f"{mem_id}: {content}")
    old_info_str = "\n".join(old_info_lines)

    return new_info_str, old_info_str


class MemUpdateChecker:
    """
    Memory update checker for detecting redundancy and conflicts between memories.

    This class uses LLM with a prompt template to analyze whether new memories
    are redundant, conflicting, or can coexist with existing memories.

    Usage:
        checker = MemUpdateChecker()
        results = await checker.check(
            new_memories={"1": "I like reading"},
            old_memories={"2": "I enjoy books"},
            base_chat_model=(model_name, model_client),
        )
    """

    def __init__(self):
        """Initialize memory checker."""
        self._prompt_applier = PromptApplier()

    async def check(
        self,
        new_memories: Dict[str, str],
        old_memories: Dict[str, str],
        base_chat_model: Model,
        retries: int = 3,
        extract_assistant_memory: bool = False,
    ) -> List[MemoryActionItem]:
        """
        Check for redundancy and conflicts between new and old memories.

        Args:
            new_memories: Dictionary of new memories to check {id: content}
            old_memories: Dictionary of existing memories {id: content}
            base_chat_model: Model instance for LLM invocation
            retries: Number of retries for LLM invocation (default: 3)
            extract_assistant_memory: Mirrors the scope-level config flag of the
                same name. When True, the batch may contain multiple distinct
                subjects (e.g. both user and assistant), so a top-priority
                "subject-first" rule is injected: facts with identical predicates
                but different subjects are kept as coexisting (none) instead of
                being mis-judged redundant/conflicting. Default False keeps the
                prompt byte-identical to the single-subject behavior.

        Returns:
            List[MemoryActionItem]: List of memories with action status (add/delete)
                - New memories with status=ADD: should be added
                - Old memories with status=DELETE: should be deleted
                - Redundant new memories are not included
        """
        # Skip checking if no old memories or no model
        if not base_chat_model:
            memory_logger.debug(
                "No need to check memories - no old memories or no model",
                event_type=LogEventType.MEMORY_PROCESS,
                metadata={"new_count": len(new_memories), "old_count": len(old_memories)},
            )
            # Return all new memories as ADD
            return [
                MemoryActionItem(id=mid, content=content, status=MemoryStatus.ADD)
                for mid, content in new_memories.items()
            ]

        # Check for exact duplicates first
        duplicate_ids = set(new_memories.keys()) & set(old_memories.keys())
        if duplicate_ids:
            memory_logger.debug(
                f"Found {len(duplicate_ids)} duplicate memory IDs",
                event_type=LogEventType.MEMORY_PROCESS,
                metadata={"duplicate_ids": list(duplicate_ids)},
            )

        # Format input for prompt
        new_info_str, old_info_str = _format_input(new_memories, old_memories)

        # Apply prompt template
        user_prompt = self._prompt_applier.apply(
            "memory_update_check",
            {
                "new_information": new_info_str,
                "old_information": old_info_str,
                "subject_emphasis": _SUBJECT_EMPHASIS if extract_assistant_memory else "",
            },
        )

        messages = [{"role": "user", "content": user_prompt}]

        memory_logger.debug(
            "Start checking memory conflicts",
            event_type=LogEventType.MEMORY_PROCESS,
            metadata={"input_messages": messages},
        )

        parser = JsonOutputParser()
        check_results = []

        for attempt in range(retries):
            try:
                response = await base_chat_model.invoke(messages=messages)
                parsed_result = await parser.parse(response.content)

                if isinstance(parsed_result, dict):
                    # Handle single object response
                    parsed_result = [parsed_result]
                elif not isinstance(parsed_result, list):
                    continue

                # Parse each result item
                for item in parsed_result:
                    check_item = MemCheckItem.model_validate(item)
                    check_results.append(check_item)

                memory_logger.debug(
                    f"Succeeded to check memories, got {len(check_results)} results",
                    event_type=LogEventType.MEMORY_PROCESS,
                    metadata={"result_count": len(check_results)},
                )
                break

            except (KeyError, ValueError) as e:
                if attempt < retries - 1:
                    memory_logger.warning(
                        f"Memory check parse error, retrying ({attempt + 1}/{retries}): {e}",
                        event_type=LogEventType.MEMORY_PROCESS,
                        exception=str(e),
                    )
                    continue
                else:
                    memory_logger.error(
                        "Memory check failed after retries",
                        event_type=LogEventType.MEMORY_PROCESS,
                        exception=str(e),
                    )
                    # Return all new memories as ADD on failure
                    return [
                        MemoryActionItem(id=mid, content=content, status=MemoryStatus.ADD)
                        for mid, content in new_memories.items()
                    ]

        # Map check results to action items
        action_items = []
        processed_new_ids = set()

        for check_item in check_results:
            new_id = check_item.info_id
            processed_new_ids.add(new_id)

            if check_item.result == CheckResult.REDUNDANT:
                # Redundant: skip, don't add to results
                memory_logger.debug(
                    f"Memory {new_id} is redundant, skipping",
                    event_type=LogEventType.MEMORY_PROCESS,
                )
                continue

            elif check_item.result == CheckResult.CONFLICTING:
                # Conflicting: add new memory with ADD status
                # and related old memories with DELETE status
                new_content = new_memories.get(new_id, check_item.info_text)
                action_items.append(MemoryActionItem(id=new_id, content=new_content, status=MemoryStatus.ADD))

                # Add related old memories with DELETE status
                for old_id, old_content in check_item.related_infos.items():
                    if old_id in old_memories:
                        action_items.append(
                            MemoryActionItem(id=old_id, content=old_content, status=MemoryStatus.DELETE)
                        )

            elif check_item.result == CheckResult.NONE:
                # No conflict: add new memory with ADD status
                new_content = new_memories.get(new_id, check_item.info_text)
                action_items.append(MemoryActionItem(id=new_id, content=new_content, status=MemoryStatus.ADD))

        memory_logger.debug(
            f"Memory check completed, returning {len(action_items)} action items",
            event_type=LogEventType.MEMORY_PROCESS,
            metadata={"action_count": len(action_items)},
        )

        return action_items
