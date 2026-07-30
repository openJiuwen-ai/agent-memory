# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from sqlalchemy import inspect, Column, String, Index, insert, delete, text
from sqlalchemy.orm import declarative_mixin, declarative_base
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.foundation.store.base_db_store import BaseDbStore
from jiuwen_memory.memory_core.migration.migration_plan import sql_registry


Base = declarative_base()


@declarative_mixin
class MessageMixin:
    message_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    scope_id = Column(String(64), nullable=False)
    content = Column(String(4096), nullable=False)
    session_id = Column(String(64), nullable=True)
    role = Column(String(32), nullable=True)
    timestamp = Column(String(32), nullable=True)


@declarative_mixin
class ScopeUserMixin:
    user_id = Column(String(64), nullable=False, primary_key=True)
    scope_id = Column(String(64), nullable=False, primary_key=True)


@declarative_mixin
class MemoryMetaMixin:
    table_name = Column(String(64), nullable=False, primary_key=True)
    schema_version = Column(String(64), nullable=False)


class UserMessage(MessageMixin, Base):
    __tablename__ = "user_message"
    # FIX-002A: Add composite index for the most common query pattern:
    # WHERE scope_id = ? ORDER BY timestamp DESC. Without this index, the
    # database performs a full table scan + filesort on large tables
    # (6.5万行 ~4s). The composite index allows index-only range scan.
    __table_args__ = (
        Index('idx_scope_timestamp', 'scope_id', 'timestamp'),
        Index('idx_user_id', 'user_id'),
    )


class ScopeUserMapping(ScopeUserMixin, Base):
    __tablename__ = "scope_user_mapping"


class MemoryMeta(MemoryMetaMixin, Base):
    __tablename__ = "memory_meta"


# Configuration for memory tables with migration information
MEMORY_TABLES_CONFIG = [
    {
        "table": UserMessage.__table__,
        "entity_key": "user_messages"
    },
    {
        "table": ScopeUserMapping.__table__,
        "entity_key": "scope_user_mapping"
    }
]


async def create_tables(
    db_store: BaseDbStore,
):
    async with db_store.get_async_engine().begin() as conn:
        newly_created_tables = []

        def check_and_create(sync_conn):
            inspector = inspect(sync_conn)
            table_name = UserMessage.__tablename__

            if inspector.has_table(table_name):
                columns = inspector.get_columns(table_name)
                column_names = [col['name'] for col in columns]

                if 'group_id' in column_names:
                    UserMessage.__table__.drop(sync_conn, checkfirst=True)
                    memory_logger.debug(f"delete old version sql table")

            for table_config in MEMORY_TABLES_CONFIG:
                if not inspector.has_table(table_config["table"].name):
                    newly_created_tables.append(table_config["table"].name)

            Base.metadata.create_all(
                sync_conn,
                tables=[
                    MemoryMeta.__table__,
                    UserMessage.__table__,
                    ScopeUserMapping.__table__
                ],
                checkfirst=True
            )

            # FIX-002A: create_all(checkfirst=True) does not add indexes to
            # already-existing tables. Explicitly create the indexes here so
            # existing databases benefit from the performance improvement.
            # CREATE INDEX IF NOT EXISTS is supported by SQLite 3.3.8+ and
            # PostgreSQL 9.5+.
            if inspector.has_table(UserMessage.__tablename__):
                existing_indexes = {
                    idx['name']
                    for idx in inspector.get_indexes(UserMessage.__tablename__)
                }
                index_ddl = [
                    "CREATE INDEX IF NOT EXISTS idx_scope_timestamp "
                    "ON user_message (scope_id, timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_user_id "
                    "ON user_message (user_id)",
                ]
                for ddl in index_ddl:
                    idx_name = ddl.split("IF NOT EXISTS ")[1].split(" ")[0]
                    if idx_name not in existing_indexes:
                        sync_conn.execute(text(ddl))
                        memory_logger.info(f"Created index: {idx_name}")

        await conn.run_sync(check_and_create)

        def update_schema_versions(sync_conn):
            inspector = inspect(sync_conn)
            
            for table_config in MEMORY_TABLES_CONFIG:
                table_name = table_config["table"].name
                entity_key = table_config["entity_key"]
                if table_name in newly_created_tables:
                    current_version = sql_registry.get_current_version(entity_key)
                    if current_version > 0:
                        insert_stmt = insert(MemoryMeta.__table__).values(
                            table_name=table_name,
                            schema_version=str(current_version)
                        )
                        sync_conn.execute(insert_stmt)

        await conn.run_sync(update_schema_versions)
