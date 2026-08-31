# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json
import hashlib
import uuid as _uuid
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.foundation.llm.schema.message import BaseMessage
from jiuwen_memory.foundation.store.base_message_store import (
    BaseMessageStore, MessageMetadata,
)
from jiuwen_memory.foundation.codec import AesStorageCodec, StorageCodec
from jiuwen_memory.memory_core.migration.migrator.memory_meta_manager import MemoryMetaManager

DEFAULT_TABLE_NAME = "user_message"
COUNT_QUERY_LIMIT = 1000000


class SqlMessageStore(BaseMessageStore):
    """
    SQL database message storage implementation
    """
    def __init__(self,
        crypto_key: Optional[bytes] = None,
        sql_db_store: object = None,
        table_name: str = DEFAULT_TABLE_NAME,
        codec: Optional[StorageCodec] = None):
        """
        Initialize SQL message storage

        Args:
            crypto_key: Encryption key (optional)
            sql_db_store: Existing SqlDbStore instance
            table_name: Message table name
            codec: Optional pre-built StorageCodec instance. When provided it
                takes precedence over crypto_key; otherwise an AesStorageCodec
                is built from crypto_key (empty key -> pass-through).
        """
        self.crypto_key = crypto_key
        self.sql_db_store = sql_db_store
        self.table_name = table_name
        self._codec = codec if codec is not None else AesStorageCodec(crypto_key or b"")

    def set_codec(self, codec: StorageCodec) -> None:
        """Inject an external codec, replacing the default AesStorageCodec."""
        self._codec = codec

    def _generate_message_id(self, message: BaseMessage, timestamp: datetime) -> str:
        """Generate a unique message ID from content, timestamp, and a random nonce.

        The random nonce ensures that concurrent requests writing identical
        messages (same content + same timestamp) produce distinct IDs,
        preventing IntegrityError collisions that previously caused silent
        data loss (Bug #3).
        """
        content_str = json.dumps(message.content, ensure_ascii=False)
        nonce = _uuid.uuid4().hex[:8]
        message_hash = hashlib.sha256(f"{content_str}{timestamp}{nonce}".encode()).hexdigest()
        return f"msg_{message_hash[:16]}_{int(timestamp.timestamp()*1000)}"

    async def add_message(self, message_add: Dict[str, Any], msg_id: str = None) -> str:
        """
        Add a single message

        Args:
            message_add: Dict containing message data

        Returns:
            str: The generated message ID
        """
        message: BaseMessage = message_add['message']
        user_id: str = message_add.get('user_id', '')
        scope_id: str = message_add.get('scope_id', '')
        session_id: str = message_add.get('session_id', '')
        timestamp: datetime = message_add.get('timestamp') or datetime.now(timezone.utc).astimezone()

        message_id = self._generate_message_id(message, timestamp)

        if msg_id:
            message_id = msg_id + "_mid"

        content = self._codec.encode(message.content)

        data = {
            'message_id': message_id,
            'user_id': user_id or '',
            'session_id': session_id or '',
            'scope_id': scope_id or '',
            'role': getattr(message, 'role', '') or '',
            'content': content,
            'timestamp': timestamp
        }

        try:
            await self.sql_db_store.write(self.table_name, data)
        except IntegrityError:
            # UNIQUE constraint conflict — extremely unlikely after nonce was
            # added to _generate_message_id, but if it does happen (e.g. a
            # different UNIQUE index), the message already exists in the DB so
            # we can safely return the ID without treating it as a failure.
            from jiuwen_memory.common.logging import memory_logger
            from jiuwen_memory.common.logging.events import LogEventType
            memory_logger.warning(
                "add_message skipped due to UNIQUE conflict",
                event_type=LogEventType.MEMORY_STORE,
                metadata={"message_id": message_id}
            )

        return message_id
    
    async def add_messages(self, message_adds: List[Dict[str, Any]]) -> List[str]:
        """
        Batch add messages

        Args:
            message_adds: List of dicts containing message data

        Returns:
            List[str]: List of generated message IDs
        """
        message_ids = []
        for message_add in message_adds:
            message_id = await self.add_message(message_add)
            message_ids.append(message_id)

        return message_ids
    
    async def get_message_by_id(self, message_id: str) -> Tuple[BaseMessage, MessageMetadata]:
        """
        Get message by message ID
        
        Args:
            message_id: Message ID
            
        Returns:
            Tuple[BaseMessage, MessageMetadata]: (message object, message metadata) tuple
            
        Raises:
            BaseError: When message does not exist
        """
        filters = {'message_id': [message_id]}
        messages = await self.sql_db_store.condition_get(table=self.table_name, conditions=filters)
        
        if not messages:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                reason=f"Message with id {message_id} not found")
        
        message_data = messages[0]
        return self._row_to_message(message_data)

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        """Parse ISO-8601 string / epoch seconds / datetime into an aware datetime.

        Returns None when the value cannot be interpreted (caller then ignores the
        time bound instead of failing the whole query).
        """
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith(('Z', 'z')):
                text = text[:-1] + '+00:00'
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(
                    tzinfo=datetime.now(timezone.utc).astimezone().tzinfo)
            return parsed
        return None

    def _build_message_where(self, message_filter: Dict[str, Any], table) -> list:
        """Build SQLAlchemy WHERE clauses from message_filter.

        Supports equality filters (user_id/scope_id/session_id) plus optional
        start_time/end_time range bounds on the timestamp column (KR-MSG-01/02).
        Also supports exclude_scopes: a list of scope_id values to exclude
        (e.g. ['middle_term_memory'] to filter out intermediate products).
        """
        clauses = []
        if message_filter.get('user_id'):
            clauses.append(table.c.user_id == message_filter['user_id'])
        if message_filter.get('scope_id'):
            clauses.append(table.c.scope_id == message_filter['scope_id'])
        if message_filter.get('session_id') is not None:
            clauses.append(table.c.session_id == message_filter['session_id'])
        # Exclude specified scopes (e.g. middle_term_memory intermediate products),
        # only when the caller has not explicitly queried that scope.
        exclude_scopes = message_filter.get('exclude_scopes')
        if exclude_scopes:
            clauses.append(table.c.scope_id.notin_(exclude_scopes))
        # NOTE: the timestamp column is String(32), storing str(aware_datetime)
        # output (space-separated, with timezone, e.g. '2026-01-01 00:01:00+00:00').
        # Comparisons must use the same string format, otherwise lexicographic
        # ordering breaks (space 0x20 < 'T' 0x54 would incorrectly exclude the
        # lower bound). Therefore _parse_ts datetime results are normalized via
        # str() to match the stored representation before binding.
        start_time = self._parse_ts(message_filter.get('start_time'))
        if start_time is not None:
            clauses.append(table.c.timestamp >= str(start_time))
        end_time = self._parse_ts(message_filter.get('end_time'))
        if end_time is not None:
            clauses.append(table.c.timestamp <= str(end_time))
        return clauses

    def _row_to_message(self, message_data: Dict[str, Any]) -> Tuple[BaseMessage, MessageMetadata]:
        """Decode one user_message row into (BaseMessage, MessageMetadata)."""
        content = self._codec.decode(message_data['content'])
        base_msg = BaseMessage(
            content=content,
            role=message_data.get('role', '')
        )
        metadata = MessageMetadata(
            message_id=message_data['message_id'],
            user_id=message_data['user_id'],
            scope_id=message_data['scope_id'],
            session_id=message_data['session_id'],
            timestamp=message_data['timestamp'],
            message_type=message_data.get('role', '')
        )
        return base_msg, metadata

    async def get_messages(
        self,
        message_filter: Dict[str, Any],
        limit: int = 10,
        order_by: str = "timestamp",
        order_direction: str = "desc",
        offset: int = 0,
    ) -> List[Tuple[BaseMessage, MessageMetadata]]:
        """
        Get messages by filter with pagination

        Args:
            message_filter: Dict with filter conditions. Optional keys
                user_id/scope_id/session_id (equality) plus start_time/end_time
                (ISO-8601 string, epoch seconds, or datetime) for timestamp range.
            limit: Maximum number of results
            order_by: Field to sort by
            order_direction: Sort direction ("asc" or "desc")
            offset: Pagination offset (KR-MSG-01); default 0 keeps legacy behavior

        Returns:
            List[Tuple[BaseMessage, MessageMetadata]]: List of (message object, message metadata) tuples
        """
        # Fast path: no time-range filter, no offset, descending order, and no exclude_scopes ->
        # keep the original equality-only code path so data-plane callers are untouched.
        # exclude_scopes requires a NOT IN condition, which get_with_sort
        # does not support — must take the range path.
        has_time_filter = (
            self._parse_ts(message_filter.get('start_time')) is not None
            or self._parse_ts(message_filter.get('end_time')) is not None
        )
        has_exclude = bool(message_filter.get('exclude_scopes'))
        if (not has_time_filter and offset <= 0 and not has_exclude
                and order_direction.lower() == "desc"):
            filters = {}
            if message_filter.get('user_id'):
                filters['user_id'] = message_filter['user_id']
            if message_filter.get('scope_id'):
                filters['scope_id'] = message_filter['scope_id']
            if message_filter.get('session_id') is not None:
                filters['session_id'] = message_filter['session_id']

            messages = await self.sql_db_store.get_with_sort(
                table=self.table_name,
                filters=filters,
                sort_by=order_by,
                order=order_direction.upper(),
                limit=limit
            )
            return [self._row_to_message(m) for m in messages]

        # Range/pagination path (KR-MSG-01): build the query directly so we can
        # apply timestamp bounds and OFFSET, which get_with_sort does not support.
        from sqlalchemy import select, and_, asc, desc

        table = await self.sql_db_store.get_table(self.table_name)
        if order_by not in table.c:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                reason=f"sort column '{order_by}' does not exist in table '{self.table_name}'",
            )
        clauses = self._build_message_where(message_filter, table)
        stmt = select(table)
        if clauses:
            stmt = stmt.where(and_(*clauses))
        order_col = table.c[order_by]
        primary_order = desc(order_col) if order_direction.lower() == "desc" else asc(order_col)
        stmt = stmt.order_by(primary_order, asc(table.c.message_id))
        if offset > 0:
            stmt = stmt.offset(offset)
        stmt = stmt.limit(limit)

        async with self.sql_db_store.async_session() as session:
            async with session.begin():
                execute_result = await session.execute(stmt)
                rows = execute_result.mappings().fetchall()
        return [self._row_to_message(dict(row)) for row in rows]

    async def update_message(self, message_id: str, content: Union[str, List[Union[str, dict]]]) -> bool:
        """
        Update message content

        Args:
            message_id: Message ID
            content: New message content

        Returns:
            bool: Whether the update was successful
        """
        encrypted_content = self._codec.encode(content)

        conditions = {'message_id': message_id}
        data = {'content': encrypted_content}

        return await self.sql_db_store.update(self.table_name, conditions, data)

    async def delete_message_by_id(self, message_id: str) -> bool:
        """
        Delete a single message by message ID

        Args:
            message_id: Message ID

        Returns:
            bool: Whether the deletion was successful
        """
        conditions = {'message_id': message_id}
        return await self.sql_db_store.delete(self.table_name, conditions)

    async def delete_messages(self, message_filter: Dict[str, Any]) -> int:
        """
        Delete messages matching the filter

        Args:
            message_filter: Dict with filter conditions

        Returns:
            int: Number of messages deleted
        """
        conditions = {}
        if message_filter.get('user_id'):
            conditions['user_id'] = message_filter['user_id']
        if message_filter.get('scope_id'):
            conditions['scope_id'] = message_filter['scope_id']
        if message_filter.get('session_id'):
            conditions['session_id'] = message_filter['session_id']

        count = await self.count_messages(message_filter)

        await self.sql_db_store.delete(self.table_name, conditions)

        return count

    async def count_messages(self, message_filter: Dict[str, Any]) -> int:
        """
        Count messages matching the filter

        Args:
            message_filter: Dict with filter conditions. Optional keys
                user_id/scope_id/session_id (equality) plus start_time/end_time
                for timestamp range (KR-MSG-02).

        Returns:
            int: Number of messages
        """
        # FIX-001A: Unified SELECT COUNT(*) path for all cases.
        # Previously, when no time-range filter and no exclude_scopes were present,
        # a "fast path" loaded up to COUNT_QUERY_LIMIT (1,000,000) full rows via
        # get_with_sort() + len() — causing severe performance degradation on
        # large tables (37.5万行 >30s, ~4GB memory peak). The range path already
        # uses SELECT COUNT(*) which is optimal for counting. _build_message_where
        # handles user_id/scope_id/session_id equality filters, so unifying all
        # paths to SELECT COUNT(*) is both correct and performant.
        from sqlalchemy import select, func, and_

        table = await self.sql_db_store.get_table(self.table_name)
        clauses = self._build_message_where(message_filter, table)
        stmt = select(func.count()).select_from(table)
        if clauses:
            stmt = stmt.where(and_(*clauses))

        async with self.sql_db_store.async_session() as session:
            async with session.begin():
                execute_result = await session.execute(stmt)
                return int(execute_result.scalar() or 0)

    async def count_by_role(self, message_filter: Dict[str, Any]) -> Dict[str, int]:
        """Count messages grouped by role (KR-MSG-02).

        Args:
            message_filter: Same filter semantics as count_messages().

        Returns:
            Dict mapping role -> count, e.g. {"user": 12, "assistant": 12}.
        """
        from sqlalchemy import select, func, and_

        table = await self.sql_db_store.get_table(self.table_name)
        clauses = self._build_message_where(message_filter, table)
        stmt = select(table.c.role, func.count()).select_from(table)
        if clauses:
            stmt = stmt.where(and_(*clauses))
        stmt = stmt.group_by(table.c.role)

        async with self.sql_db_store.async_session() as session:
            async with session.begin():
                execute_result = await session.execute(stmt)
                rows = execute_result.all()

        return {(role if role else "unknown"): int(count) for role, count in rows}

    async def get_schema_version(self) -> int | None:
        """
        Get the current schema version of the message store.

        Returns:
            int | None: Current version number or None if not set
        """
        meta_manager = MemoryMetaManager(self.sql_db_store)
        result = await meta_manager.get_by_table_name(self.table_name)
        if result and len(result) > 0:
            version_str = result[0].get('schema_version')
            if version_str:
                return int(version_str)
        return None

    async def set_schema_version(self, version: int) -> None:
        """
        Set the schema version of the message store.

        Args:
            version: New version number to store
        """
        meta_manager = MemoryMetaManager(self.sql_db_store)
        await meta_manager.add(self.table_name, str(version))
