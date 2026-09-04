"""本地 md 视图存储（``LocalMarkdownStore``）——路径分流、落盘回填与块替换/删除。

md 是文档记忆的人类可读视图，落盘路径由 memory_class + coords.project 映射（F08 §3），
``write`` 落盘后回填 ``unit.system_metadata[md_filename]`` 供影子索引落库。失效方向：

- memory_class 空未兜底 → md 路径与影子索引 category 列落值分叉，召回按 project+category
  隔离时丢条目。
- ``replace_content`` / ``remove_content`` 块定位靠「标题行后的正文 == 目标 content」，
  正文多行或含换行会让切块失锚——文档路径入口已 `_sanitize_document_content` 折叠单行。
- 回填的 md_filename 是相对根路径，写坏会导致影子索引 md_filename 列与看门狗定位失锚。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为

import datetime

import pytest

from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    MD_FILENAME_KEY,
    MD_TITLE_KEY,
    MEMORY_CLASS_KEY,
    MemoryUnit,
    Scope,
    Segment,
)
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


def _store(tmp_path) -> LocalMarkdownStore:
    return LocalMarkdownStore(root=str(tmp_path))


def _unit(uid: str, content: str, metadata: dict | None = None) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=SCOPE,
        segments=[Segment(content=content)],
        system_metadata=dict(metadata or {}),
    )


# -- 路径映射（F08 §3） ------------------------------------------------------ #


def test_md_path_for_user_memory_ignores_project() -> None:
    """user_memory 跨 project 放 memory 根下 USER.md。"""
    store = _store(None)
    unit = _unit("u1", "x", {MEMORY_CLASS_KEY: "user_memory", COORDS_KEY: {"project": "p1"}})
    assert store._md_path(unit) == "memory/USER.md"


def test_md_path_for_project_memory_scopes_by_project() -> None:
    store = _store(None)
    unit = _unit("u1", "x", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})
    assert store._md_path(unit) == "memory/p1/MEMORY.md"


def test_md_path_for_team_memory_uses_daily_file() -> None:
    store = _store(None)
    unit = _unit("u1", "x", {MEMORY_CLASS_KEY: "team_memory", COORDS_KEY: {"project": "p1"}})
    today = datetime.date.today().isoformat()
    assert store._md_path(unit) == f"memory/p1/daily_memory/{today}.md"


def test_md_path_defaults_missing_class_and_project() -> None:
    """空 memory_class → team_memory；空 project → default（F08 §2/§3 兜底）。"""
    store = _store(None)
    unit = _unit("u1", "x", {})
    today = datetime.date.today().isoformat()
    assert store._md_path(unit) == f"memory/default/daily_memory/{today}.md"


def test_unknown_memory_class_falls_back_to_team_memory() -> None:
    store = _store(None)
    unit = _unit("u1", "x", {MEMORY_CLASS_KEY: "weird", COORDS_KEY: {"project": "p1"}})
    today = datetime.date.today().isoformat()
    assert store._md_path(unit) == f"memory/p1/daily_memory/{today}.md"


def test_resolved_memory_class_defaults_to_team_memory() -> None:
    assert LocalMarkdownStore._resolved_memory_class({}) == "team_memory"
    assert LocalMarkdownStore._resolved_memory_class({MEMORY_CLASS_KEY: ""}) == "team_memory"
    assert LocalMarkdownStore._resolved_memory_class({MEMORY_CLASS_KEY: "project_memory"}) == "project_memory"


def test_project_of_reads_coords_dict_only() -> None:
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {COORDS_KEY: {"project": "p1"}})) == "p1"
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {COORDS_KEY: {"project": ""}})) == "default"
    # coords 非 dict（如字符串）→ default，不抛错
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {COORDS_KEY: "p1"})) == "default"
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {})) == "default"


# -- 渲染 -------------------------------------------------------------------- #


def test_render_block_uses_md_title_for_non_daily_files() -> None:
    unit = _unit("u1", "hello", {MD_TITLE_KEY: "Frontend framework"})
    block = LocalMarkdownStore._render_block(unit, "hello", "project_memory")
    assert block == "# Frontend framework\nhello\n\n"


def test_render_block_falls_back_to_unit_id_when_no_title() -> None:
    unit = _unit("u1", "hello", {})
    block = LocalMarkdownStore._render_block(unit, "hello", "project_memory")
    assert block == "# u1\nhello\n\n"


def test_render_block_uses_team_coord_for_daily_files() -> None:
    unit = _unit("u1", "hello", {COORDS_KEY: {"team": "infra"}})
    block = LocalMarkdownStore._render_block(unit, "hello", "team_memory")
    assert block == "# infra\nhello\n\n"


def test_render_block_falls_back_to_unit_id_when_no_team() -> None:
    unit = _unit("u1", "hello", {})
    block = LocalMarkdownStore._render_block(unit, "hello", "team_memory")
    assert block == "# u1\nhello\n\n"


# -- write 落盘与回填 -------------------------------------------------------- #


def test_write_persists_blocks_and_backfills_md_filename(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "hello", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})

    store.write(SCOPE, [unit])

    assert unit.system_metadata[MD_FILENAME_KEY] == "memory/p1/MEMORY.md"
    # 兜底后的 memory_class 写回
    assert unit.system_metadata[MEMORY_CLASS_KEY] == "project_memory"
    written = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "# u1\nhello\n\n" in written


def test_write_backfills_default_class_when_missing(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "hello", {})  # 无 memory_class / coords

    store.write(SCOPE, [unit])

    assert unit.system_metadata[MEMORY_CLASS_KEY] == "team_memory"
    today = datetime.date.today().isoformat()
    assert unit.system_metadata[MD_FILENAME_KEY] == f"memory/default/daily_memory/{today}.md"


def test_write_groups_same_file_units_into_one_append(tmp_path) -> None:
    store = _store(tmp_path)
    units = [
        _unit("u1", "first", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
        _unit("u2", "second", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
    ]

    store.write(SCOPE, units)

    text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "# u1\nfirst\n\n" in text
    assert "# u2\nsecond\n\n" in text


# -- replace_content --------------------------------------------------------- #


def test_replace_content_replaces_matching_block(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "old", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})
    store.write(SCOPE, [unit])

    replaced = store.replace_content(SCOPE, "memory/p1/MEMORY.md", "old", "new")

    assert replaced is True
    text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "# u1\nnew\n\n" in text
    assert "old" not in text


def test_replace_content_returns_false_when_no_match(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "hello", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})
    store.write(SCOPE, [unit])

    assert store.replace_content(SCOPE, "memory/p1/MEMORY.md", "not there", "x") is False


def test_replace_content_returns_false_when_file_missing(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.replace_content(SCOPE, "memory/nope/MEMORY.md", "a", "b") is False


# -- remove_content ---------------------------------------------------------- #


def test_remove_content_deletes_matching_block(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(SCOPE, [
        _unit("u1", "keep", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
        _unit("u2", "drop", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
    ])

    removed = store.remove_content(SCOPE, "memory/p1/MEMORY.md", "drop")

    assert removed is True
    text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "drop" not in text
    assert "# u1\nkeep\n\n" in text


def test_remove_content_returns_false_when_no_match(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(SCOPE, [_unit("u1", "hello", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})])

    assert store.remove_content(SCOPE, "memory/p1/MEMORY.md", "missing") is False


# -- 契约 -------------------------------------------------------------------- #


def test_store_type_and_health(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.store_type() is StoreType.MARKDOWN
    assert store.health() is None


def test_health_reports_missing_root() -> None:
    from jiuwen_memory.common.errors import HealthCheckError

    store = LocalMarkdownStore(root="")  # 空 root 解析为非目录
    with pytest.raises(HealthCheckError):
        store.health()
