# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.dialects import registry
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from foundation.store.db.gauss_db_store import GaussDbStore
from memory_core.manage.mem_model.db_model import UserMessage, create_tables


def test_gauss_db_store_returns_registered_async_engine():
    engine = object()
    store = GaussDbStore(engine)

    assert store.get_async_engine() is engine


def test_importing_gauss_db_store_registers_gaussdb_dialects():
    cls1 = registry.load("gaussdb")
    cls2 = registry.load("gaussdb.async_gaussdb")

    assert cls1 is cls2
    assert cls1.name == "gaussdb"
    assert cls1.driver == "async_gaussdb"


@pytest.mark.asyncio
async def test_gauss_db_store_crud_operations():
    url = os.getenv("GAUSSDB_URL")
    if not url:
        pytest.skip("GAUSSDB_URL is not set; skipping GaussDB integration test")

    engine = create_async_engine(url, echo=False)
    store = GaussDbStore(async_conn=engine)
    session_factory = sessionmaker(store.get_async_engine(), class_=AsyncSession, expire_on_commit=False)
    message_id = f"test-crud-{uuid.uuid4().hex}"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        await create_tables(store)

        async with session_factory() as session:
            await session.execute(delete(UserMessage).where(UserMessage.message_id == message_id))
            await session.commit()

        async with session_factory() as session:
            session.add(UserMessage(
                message_id=message_id,
                user_id="u1",
                scope_id="sc1",
                session_id="ss1",
                role="user",
                content="hello gaussdb",
                timestamp=timestamp,
            ))
            await session.commit()

        async with session_factory() as session:
            row = (await session.execute(
                select(UserMessage).where(UserMessage.message_id == message_id)
            )).scalar_one()
            assert row.content == "hello gaussdb"

        async with session_factory() as session:
            await session.execute(
                update(UserMessage)
                .where(UserMessage.message_id == message_id)
                .values(content="hello gaussdb v2")
            )
            await session.commit()

        async with session_factory() as session:
            row = (await session.execute(
                select(UserMessage).where(UserMessage.message_id == message_id)
            )).scalar_one()
            assert row.content == "hello gaussdb v2"

        async with session_factory() as session:
            await session.execute(delete(UserMessage).where(UserMessage.message_id == message_id))
            await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(
                select(UserMessage).where(UserMessage.message_id == message_id)
            )).all()
            assert rows == []

    finally:
        await engine.dispose()
