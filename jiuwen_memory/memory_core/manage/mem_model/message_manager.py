# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from datetime import datetime, timezone
from typing import Any, Dict, Tuple, Optional

from pydantic import BaseModel, Field

from jiuwen_memory.foundation.store.base_message_store import BaseMessageStore, MessageMetadata
from jiuwen_memory.foundation.llm import BaseMessage
from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error, BaseError


class MessageAddRequest(BaseModel):
    user_id: Optional[str] = None
    scope_id: Optional[str] = None
    content: Optional[str] = None
    role: Optional[str] = None
    session_id: Optional[str] = None
    timestamp: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc).astimezone())


class MessageManager:
    def __init__(self, store: BaseMessageStore):
        self._store = store

    @property
    def store(self) -> BaseMessageStore:
        return self._store

    async def add(self, req: MessageAddRequest, msg_id: str = None) -> str:
        if req.user_id is None:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg=f"must provide user_id for add message",
            )
        if req.scope_id is None:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg=f"must provide scope_id for add message",
            )
        if req.content is None:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg=f"must provide content for add message",
            )

        message = BaseMessage(content=req.content, role=req.role)
        message_add: Dict[str, Any] = {
            'message': message,
            'user_id': req.user_id,
            'scope_id': req.scope_id,
            'session_id': req.session_id,
            'timestamp': req.timestamp
        }
        return await self._store.add_message(message_add, msg_id)

    async def get(self, user_id: str = None, scope_id: str = None, session_id: str = None,
                    message_len: int = 10) -> list[Tuple[BaseMessage, datetime, str]]:
        if message_len <= 0:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg=f"message length Must bigger than zero for get message",
            )

        message_filter: Dict[str, Any] = {
            'user_id': user_id,
            'scope_id': scope_id,
            'session_id': session_id,
        }

        messages_with_metadata = await self._store.get_messages(
            message_filter, limit=message_len, order_direction='desc'
        )

        result = []
        for message, metadata in reversed(messages_with_metadata):
            result.append((message, metadata.timestamp, metadata.message_id))
        
        return result

    async def get_with_metadata(self, user_id: str = None, scope_id: str = None, session_id: str = None,
                                message_len: int = 10) -> list[Tuple[BaseMessage, MessageMetadata]]:
        """Like ``get`` but also returns the full ``MessageMetadata`` (user_id/scope_id/session_id).

        The background middle->long-term sweep needs the original owner of each middle
        message; ``get`` strips metadata down to (message, timestamp, message_id) which
        loses it. This returns what the store already yields, oldest-first (same order as
        ``get``).
        """
        if message_len <= 0:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg=f"message length Must bigger than zero for get message",
            )

        message_filter: Dict[str, Any] = {
            'user_id': user_id,
            'scope_id': scope_id,
            'session_id': session_id,
        }

        messages_with_metadata = await self._store.get_messages(
            message_filter, limit=message_len, order_direction='desc'
        )
        # oldest-first, consistent with `get`
        return list(reversed(messages_with_metadata))

    async def get_message_meta_by_id(self, msg_id: str) -> Optional[Tuple[BaseMessage, MessageMetadata]]:
        """Get a single message with its full metadata by id, or None if missing.

        Wraps ``store.get_message_by_id`` without stripping metadata (unlike
        ``get_by_id`` which only returns timestamp). Used to recover the real scope_id
        of a middle message from its original (non-middle) twin.

        Note: ``store.get_message_by_id`` signals "not found" via ``build_error``
        (a ``BaseError`` subclass, not ``ValueError``); KR-MSG-04 relies on this
        returning None so the endpoint can answer 404 instead of 500.
        """
        try:
            return await self._store.get_message_by_id(msg_id)
        except (ValueError, BaseError):
            return None

    async def get_by_id(self, msg_id: str) -> Tuple[BaseMessage, datetime] | None:
        try:
            message, metadata = await self._store.get_message_by_id(msg_id)
            return message, metadata.timestamp
        except ValueError:
            return None

    async def delete_by_user_and_scope(self, user_id: str, scope_id: str) -> bool:
        message_filter: Dict[str, Any] = {
            'user_id': user_id,
            'scope_id': scope_id
        }

        count = await self._store.delete_messages(message_filter)
        return count > 0

    async def delete_by_id(self, msg_id: str) -> bool:
        """Delete a single message by its ID."""
        return await self._store.delete_message_by_id(msg_id)