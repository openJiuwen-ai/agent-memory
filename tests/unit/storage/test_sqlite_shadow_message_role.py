# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F16 存储侧：SQLite 影子索引可空 message_role 预过滤与文档同步清除单测。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import (
    MESSAGE_ROLE_KEY,
    FilterClause,
    FilterOp,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    RetrievalPipeline,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.storage.domain_store_impl.composite_domain_store import CompositeDomainStore
from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import (
    SqliteDocumentShadowIndex,
    _compile_system_filters,
    _requires_vector_prefilter,
)
from jiuwen_memory.storage.store_manager_impl.composite_store_manager import CompositeStoreManager
from jiuwen_memory.storage.types import TextQuery, VectorQuery

pytestmark = pytest.mark.unit


def _make_unit(uid: str, scope: Scope, text: str, *, role: str | None = None) -> MemoryUnit:
    now = datetime.now(UTC)
    sys_meta = {"project": ""}
    if role is not None:
        sys_meta[MESSAGE_ROLE_KEY] = role
    return MemoryUnit(
        id=uid,
        scope=scope,
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=text)],
        temporal=Temporal(t_ingest=now, t_valid=now),
        system_metadata=sys_meta,
        lifecycle=LifecycleState.ACTIVE,
    )


def test_sqlite_shadow_compiles_nullable_message_role_and_prefilter():
    eq_sql, eq_params = _compile_system_filters(
        FilterClause("system_metadata.message_role", FilterOp.EQ, "assistant")
    )
    assert eq_sql is not None and "COALESCE" not in eq_sql
    assert eq_params == ["assistant"]

    ne_sql, ne_params = _compile_system_filters(
        FilterClause("system_metadata.message_role", FilterOp.NE, "assistant")
    )
    assert ne_sql is not None and "IS NULL OR" in ne_sql
    assert ne_params == ["assistant"]

    not_in_sql, not_in_params = _compile_system_filters(
        FilterClause("system_metadata.message_role", FilterOp.NOT_IN, ["", "assistant"])
    )
    assert not_in_sql is not None and "IS NULL OR" in not_in_sql
    assert not_in_params == ["", "assistant"]

    gt_sql, gt_params = _compile_system_filters(
        FilterClause("system_metadata.message_role", FilterOp.GT, 1)
    )
    assert gt_sql is None and not gt_params, "可空字符串列范围算子不下推 SQL，交后置复核"

    assert _requires_vector_prefilter(
        FilterClause("system_metadata.message_role", FilterOp.EQ, "planner")
    )


def test_sqlite_shadow_fulltext_and_vector_filter_by_message_role(tmp_path):
    tokenizer = WhitespaceTokenizer()
    embedder = HashingEmbedder(tokenizer, dim=16)
    shadow = SqliteDocumentShadowIndex(
        db_path=str(tmp_path / "shadow.db"),
        tokenizer=tokenizer,
        embedder=embedder,
    )
    scope = Scope(org="o", space="s", user="u", agent="a")
    try:
        u_asst = _make_unit("u-asst", scope, "alpha release plan friday", role="assistant")
        u_user = _make_unit("u-user", scope, "alpha release plan maybe", role="user")
        u_custom = _make_unit("u-plan", scope, "alpha release plan freeze api", role="planner")
        u_plain = _make_unit("u-plain", scope, "alpha release plan note", role=None)
        shadow.insert_units(scope, [u_asst, u_user, u_custom, u_plain])

        # FTS EQ
        fts_asst = shadow.search_fulltext(
            scope,
            TextQuery(
                text="alpha release plan",
                top_k=10,
                filters=FilterClause("system_metadata.message_role", FilterOp.EQ, "assistant"),
            ),
        )
        assert [r.id for r in fts_asst] == ["u-asst"]

        # FTS EQ "" 不应捞出无 role 行
        fts_empty = shadow.search_fulltext(
            scope,
            TextQuery(
                text="alpha release plan",
                top_k=10,
                filters=FilterClause("system_metadata.message_role", FilterOp.EQ, ""),
            ),
        )
        assert not fts_empty, "EQ '' 不得命中无 message_role 行"

        # FTS NE "" 应命中所有含角色和无角色行
        fts_ne_empty = shadow.search_fulltext(
            scope,
            TextQuery(
                text="alpha release plan",
                top_k=10,
                filters=FilterClause("system_metadata.message_role", FilterOp.NE, ""),
            ),
        )
        assert {r.id for r in fts_ne_empty} == {"u-asst", "u-user", "u-plan", "u-plain"}

        # FTS NOT_IN ["", "assistant"] 应保留无 role 行与其余角色
        fts_not_in = shadow.search_fulltext(
            scope,
            TextQuery(
                text="alpha release plan",
                top_k=10,
                filters=FilterClause(
                    "system_metadata.message_role", FilterOp.NOT_IN, ["", "assistant"]
                ),
            ),
        )
        assert {r.id for r in fts_not_in} == {"u-user", "u-plan", "u-plain"}

        # 向量预过滤（若 sqlite-vec 可用）
        if shadow.vec_enabled:
            vec = embedder.embed(["alpha release plan"])[0]
            vec_plan = shadow.search_vector(
                scope,
                VectorQuery(
                    vector=vec,
                    top_k=2,
                    filters=FilterClause("system_metadata.message_role", FilterOp.EQ, "planner"),
                ),
            )
            assert [r.id for r in vec_plan] == ["u-plan"]
            vec_ne = shadow.search_vector(
                scope,
                VectorQuery(
                    vector=vec,
                    top_k=10,
                    filters=FilterClause(
                        "system_metadata.message_role", FilterOp.NE, "assistant"
                    ),
                ),
            )
            assert "u-plain" in {r.id for r in vec_ne}
            assert "u-asst" not in {r.id for r in vec_ne}
    finally:
        shadow.close()


def test_edit_md_file_aligned_supersede_clears_message_role(tmp_path):
    md_store = LocalMarkdownStore(root=str(tmp_path / "docs"))
    shadow = SqliteDocumentShadowIndex(
        db_path=str(tmp_path / "shadow.db"),
        tokenizer=WhitespaceTokenizer(),
        embedder=None,
    )
    manager = CompositeStoreManager(
        markdown={"default": md_store},
        shadow_index={"default": shadow},
    )
    domain = CompositeDomainStore(
        manager=manager,
        preferred_pipeline=next(iter(RetrievalPipeline)),
        write_document=True,
    )
    scope = Scope(org="o", space="s", user="u", agent="a")
    try:
        u = _make_unit("u-doc-1", scope, "Original assistant statement", role="assistant")
        u.system_metadata["memory_class"] = "long_term_memory"
        domain.add(scope, [u])
        stored_before = domain.list(scope).items
        assert len(stored_before) == 1
        assert stored_before[0].system_metadata.get(MESSAGE_ROLE_KEY) == "assistant"

        updated = domain.edit_md_file(
            scope,
            "MEMORY.md",
            old_text="Original assistant statement",
            new_text="Human edited statement",
        )
        assert updated, "edit_md_file 应返回新建替代单元"
        assert all(MESSAGE_ROLE_KEY not in item.system_metadata for item in updated), (
            "文档编辑改写正文后应清除继承的 message_role"
        )
    finally:
        shadow.close()
