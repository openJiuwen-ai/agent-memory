"""文档看门狗（``LocalWatchdog``）——md 路径反推与 unit 粒度增量同步。

看门狗监听 md 变更，把用户手改同步进影子索引：``_sync_one`` 拿旧 (unit_id, content_hash)
与按行算出的新 hash diff，只动变化的 unit（删旧 + 建新，unit 粒度而非整文件重建）。失效方向：

- md 路径反推 project/category 错（如 USER.md 误判进 project 子目录）→ 新建 unit 落错
  归属坐标，召回按 project 隔离丢失。
- ``_collect_new_contents`` 未跳过标题行（``#``）/空行 → 把标题当正文建幽灵 unit、空行
  参与 hash diff 制造假漂移。
- 整文件重建（而非 unit 粒度）会丢失其他 unit 的 id，破坏 supersedes 版本链（F07 §12）。

本文件测纯函数与 ``_sync_one`` 的三类 diff（纯新增 / 纯删除 / 改一行换 id）。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为

import hashlib
import os

import pytest

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import COORDS_KEY, MD_FILENAME_KEY, MEMORY_CLASS_KEY, Scope
from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
from jiuwen_memory.storage.watchdog_impl.local_watchdog import (
    LocalWatchdog,
    _class_from_md_path,
    _content_hash,
    _project_from_md_path,
)

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme")


def _watchdog(tmp_path) -> LocalWatchdog:
    return LocalWatchdog(
        shadow=SqliteDocumentShadowIndex(
            db_path=str(tmp_path / "shadow.db"), tokenizer=WhitespaceTokenizer()
        ),
        md_store=LocalMarkdownStore(root=str(tmp_path)),
        markdown_root=str(tmp_path),
        scope=SCOPE,
    )


# -- md 路径反推 ------------------------------------------------------------- #


def test_content_hash_matches_shadow_index_sha256() -> None:
    assert _content_hash("hello") == hashlib.sha256(b"hello").hexdigest()


def test_project_from_md_path_for_user_file_is_default() -> None:
    assert _project_from_md_path("memory/USER.md") == "default"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("memory/p1/MEMORY.md", "p1"),
        ("memory/p1/daily_memory/2026-09-01.md", "p1"),
        (r"memory\p1\MEMORY.md", "p1"),  # 反斜杠归一
        ("memory/p1", "p1"),
    ],
)
def test_project_from_md_path_scopes_by_project(path: str, expected: str) -> None:
    assert _project_from_md_path(path) == expected


def test_project_from_md_path_unknown_shape_defaults() -> None:
    assert _project_from_md_path("other/file.md") == "default"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("memory/p1/daily_memory/x.md", "team_memory"),
        ("memory/USER.md", "user_memory"),
        ("memory/p1/MEMORY.md", "project_memory"),
        ("memory/p1/unknown.md", "team_memory"),  # 未知兜底
    ],
)
def test_class_from_md_path_maps_file_to_memory_class(path: str, expected: str) -> None:
    assert _class_from_md_path(path) == expected


# -- 按行收集 ---------------------------------------------------------------- #


def test_collect_new_contents_skips_titles_blanks_and_dedups(tmp_path) -> None:
    md = tmp_path / "memory" / "p1" / "MEMORY.md"
    md.parent.mkdir(parents=True)
    md.write_text("# title\nhello\n\n# title2\nhello\n\nworld\n", encoding="utf-8")

    wd = _watchdog(tmp_path)
    result = wd._collect_new_contents(str(md))

    # 标题行跳过；空行跳过；重复 content 去重；保序
    assert [content for _, content in result] == ["hello", "world"]
    assert [h for h, _ in result] == [_content_hash("hello"), _content_hash("world")]


# -- 建 unit 缺省元数据 ------------------------------------------------------ #


def test_build_unit_derives_coords_and_class_from_md_path(tmp_path) -> None:
    wd = _watchdog(tmp_path)
    unit = wd._build_unit("memory/p1/MEMORY.md", "hello world")

    assert unit.scope is SCOPE
    assert unit.segments[0].content == "hello world"
    assert unit.system_metadata[COORDS_KEY] == {"project": "p1"}
    assert unit.system_metadata[MEMORY_CLASS_KEY] == "project_memory"
    assert unit.system_metadata[MD_FILENAME_KEY] == "memory/p1/MEMORY.md"
    assert unit.provenance == ["watchdog_sync"]


# -- 相对路径转换 ------------------------------------------------------------ #


def test_to_rel_path_normalizes_into_memory_prefix(tmp_path) -> None:
    wd = _watchdog(tmp_path)
    abs_path = str(tmp_path / "memory" / "p1" / "MEMORY.md")
    assert wd._to_rel_path(abs_path) == "memory/p1/MEMORY.md"


def test_to_rel_path_rejects_escape(tmp_path) -> None:
    wd = _watchdog(tmp_path)
    outside = os.path.join(str(tmp_path), "..", "other.md")
    assert wd._to_rel_path(outside) is None


# -- _sync_one 增量同步 ------------------------------------------------------ #


def test_sync_one_inserts_new_lines(tmp_path) -> None:
    """md 出现影子索引没有的行 → 建新 unit（纯新增）。"""
    wd = _watchdog(tmp_path)
    md = tmp_path / "memory" / "p1" / "MEMORY.md"
    md.parent.mkdir(parents=True)
    md.write_text("# t\nhello world\n\n", encoding="utf-8")

    wd._sync_one("memory/p1/MEMORY.md", deleted=False)

    units = wd._shadow.get_units(SCOPE, [u[0] for u in wd._shadow.list_units_by_md(SCOPE, "memory/p1/MEMORY.md")])
    assert [u.segments[0].content for u in units] == ["hello world"]


def test_sync_one_deletes_removed_lines(tmp_path) -> None:
    """md 删除影子索引登记的行 → 删对应 unit（纯删除）。"""
    wd = _watchdog(tmp_path)
    from jiuwen_memory.common.type_def import MemoryUnit, Segment

    md_filename = "memory/p1/MEMORY.md"
    old = MemoryUnit(
        id="u1",
        scope=SCOPE,
        segments=[Segment(content="gone content")],
        system_metadata={MD_FILENAME_KEY: md_filename},
    )
    wd._shadow.insert_units(SCOPE, [old])
    # md 文件空（对应行已删）
    md = tmp_path / "memory" / "p1" / "MEMORY.md"
    md.parent.mkdir(parents=True)
    md.write_text("", encoding="utf-8")

    wd._sync_one(md_filename, deleted=False)

    assert wd._shadow.get_units(SCOPE, ["u1"]) == []


def test_sync_one_replaces_changed_line_with_new_id(tmp_path) -> None:
    """改一行 → 旧 hash 消失 + 新 hash 出现 → 删旧 unit + 建新 unit（新 id）。"""
    wd = _watchdog(tmp_path)
    from jiuwen_memory.common.type_def import MemoryUnit, Segment

    md_filename = "memory/p1/MEMORY.md"
    old = MemoryUnit(
        id="u-old",
        scope=SCOPE,
        segments=[Segment(content="old content")],
        system_metadata={MD_FILENAME_KEY: md_filename},
    )
    wd._shadow.insert_units(SCOPE, [old])
    md = tmp_path / "memory" / "p1" / "MEMORY.md"
    md.parent.mkdir(parents=True)
    md.write_text("# t\nnew content\n\n", encoding="utf-8")

    wd._sync_one(md_filename, deleted=False)

    assert wd._shadow.get_units(SCOPE, ["u-old"]) == []
    remaining = wd._shadow.list_units_by_md(SCOPE, md_filename)
    assert len(remaining) == 1
    new_units = wd._shadow.get_units(SCOPE, [remaining[0][0]])
    assert new_units[0].segments[0].content == "new content"
    assert new_units[0].id != "u-old"  # 新 uuid，不保留旧 id


def test_sync_one_noop_when_in_sync(tmp_path) -> None:
    """md 与索引一致时无事可做，不产生额外 insert/delete。"""
    wd = _watchdog(tmp_path)
    md = tmp_path / "memory" / "p1" / "MEMORY.md"
    md.parent.mkdir(parents=True)
    md.write_text("# t\nhello\n\n", encoding="utf-8")

    wd._sync_one("memory/p1/MEMORY.md", deleted=False)  # 首次 insert
    before = wd._shadow.list_units_by_md(SCOPE, "memory/p1/MEMORY.md")
    wd._sync_one("memory/p1/MEMORY.md", deleted=False)  # 再次：无漂移
    after = wd._shadow.list_units_by_md(SCOPE, "memory/p1/MEMORY.md")

    assert before == after


def test_sync_one_deleted_flag_drops_all_units(tmp_path) -> None:
    """文件删除（deleted=True）→ 该文件所有 unit 全删。"""
    wd = _watchdog(tmp_path)
    from jiuwen_memory.common.type_def import MemoryUnit, Segment

    md_filename = "memory/p1/MEMORY.md"
    wd._shadow.insert_units(
        SCOPE,
        [
            MemoryUnit(id="u1", scope=SCOPE, 
                       segments=[Segment(content="a")], system_metadata={MD_FILENAME_KEY: md_filename}),
            MemoryUnit(id="u2", scope=SCOPE, 
                       segments=[Segment(content="b")], system_metadata={MD_FILENAME_KEY: md_filename}),
        ],
    )

    wd._sync_one(md_filename, deleted=True)

    assert wd._shadow.get_units(SCOPE, ["u1", "u2"]) == []
