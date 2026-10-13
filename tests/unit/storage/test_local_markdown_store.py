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
import os

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


def test_md_path_for_project_memory_without_project_lands_at_memory_root() -> None:
    """project_memory + coords={}（无 project）→ 落 memory 根下 MEMORY.md，与 USER.md 同目录。

    agent-core provider 无 project 时传 coords={}（空字典）请求判定；判定为 project_memory 后，
    md 落点取 coords.project，空串即落 memory 根下（跨项目可见）——不是 default/ 子目录。与
    user_memory 的 USER.md 同落 memory 根下，是「无 project 同目录」契约的直接守卫：一旦
    _md_path 改成空 project 进 default/ 或别的子目录，这里立即捕获。
    """
    store = _store(None)
    proj = _unit("u1", "x", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {}})
    user = _unit("u2", "y", {MEMORY_CLASS_KEY: "user_memory", COORDS_KEY: {}})
    assert store._md_path(proj) == "memory/MEMORY.md"
    assert store._md_path(user) == "memory/USER.md"
    # 同落 memory 根下：路径形如 memory/<file>，不进 default/ 或 project 子目录
    for path in (store._md_path(proj), store._md_path(user)):
        assert "default" not in path
        assert path.startswith("memory/") and path.count("/") == 1


def test_md_path_for_team_memory_uses_daily_file() -> None:
    store = _store(None)
    unit = _unit("u1", "x", {MEMORY_CLASS_KEY: "team_memory", COORDS_KEY: {"project": "p1"}})
    today = datetime.date.today().isoformat()
    assert store._md_path(unit) == f"memory/p1/daily_memory/{today}.md"


def test_md_path_defaults_missing_class_and_project() -> None:
    """空 memory_class → team_memory；空 project → 落 memory 根下（跨项目可见）。"""
    store = _store(None)
    unit = _unit("u1", "x", {})
    today = datetime.date.today().isoformat()
    assert store._md_path(unit) == f"memory/daily_memory/{today}.md"


def test_unknown_memory_class_falls_back_to_team_memory() -> None:
    store = _store(None)
    unit = _unit("u1", "x", {MEMORY_CLASS_KEY: "weird", COORDS_KEY: {"project": "p1"}})
    today = datetime.date.today().isoformat()
    assert store._md_path(unit) == f"memory/p1/daily_memory/{today}.md"


def test_md_path_for_agent_llm_classes_share_routes() -> None:
    """agent_llm 抽取产出的三类（F09）与 Router 判定三类共享同一路径。

    user_profile↔user_memory（跨 project USER.md）、long_term_memory↔project_memory
    （{project}/MEMORY.md，无 project 落 memory 根下）、daily_memory↔team_memory
    （{project}/daily_memory/日期.md）。
    """
    store = _store(None)
    today = datetime.date.today().isoformat()
    profile = _unit("u1", "x", {MEMORY_CLASS_KEY: "user_profile", COORDS_KEY: {"project": "p1"}})
    long_term = _unit(
        "u2", "y", {MEMORY_CLASS_KEY: "long_term_memory", COORDS_KEY: {"project": "p1"}}
    )
    long_term_root = _unit("u3", "z", {MEMORY_CLASS_KEY: "long_term_memory", COORDS_KEY: {}})
    daily = _unit("u4", "w", {MEMORY_CLASS_KEY: "daily_memory", COORDS_KEY: {"project": "p1"}})
    assert store._md_path(profile) == "memory/USER.md"
    assert store._md_path(long_term) == "memory/p1/MEMORY.md"
    assert store._md_path(long_term_root) == "memory/MEMORY.md"
    assert store._md_path(daily) == f"memory/p1/daily_memory/{today}.md"


def test_resolved_memory_class_defaults_to_team_memory() -> None:
    assert LocalMarkdownStore._resolved_memory_class({}) == "team_memory"
    assert LocalMarkdownStore._resolved_memory_class({MEMORY_CLASS_KEY: ""}) == "team_memory"
    assert (
        LocalMarkdownStore._resolved_memory_class({MEMORY_CLASS_KEY: "project_memory"})
        == "project_memory"
    )


def test_project_of_reads_scalar_or_coords() -> None:
    # agent 场景：project 作标量直传 system_metadata（coords dict 会被标量校验拒）
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {"project": "p1"})) == "p1"
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {"project": ""})) == ""
    # router 场景：coords.project（_route 回写）
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {COORDS_KEY: {"project": "p1"}})) == "p1"
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {COORDS_KEY: {"project": ""}})) == ""
    # coords 非 dict（如字符串）→ 空串兜底，不抛错
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {COORDS_KEY: "p1"})) == ""
    assert LocalMarkdownStore._project_of(_unit("u1", "x", {})) == ""
    # scalar 优先于 coords（两者并有时标量胜出）
    assert (
        LocalMarkdownStore._project_of(
            _unit("u1", "x", {"project": "s", COORDS_KEY: {"project": "c"}})
        )
        == "s"
    )


# -- 渲染 -------------------------------------------------------------------- #


def test_render_block_uses_md_title_for_non_daily_files() -> None:
    unit = _unit("u1", "hello", {MD_TITLE_KEY: "Frontend framework"})
    block = LocalMarkdownStore._render_block(unit, "hello", "project_memory")
    assert block == "# Frontend framework\nhello\n\n"


def test_render_block_falls_back_to_empty_title_when_no_title() -> None:
    # 空标题不再用 unit_id 兜底——标题行保留作 block 结构锚点（split("\n",1)），
    # 内容空串；read-back 路径不解析标题内容，shadow 不存标题，无副作用。
    unit = _unit("u1", "hello", {})
    block = LocalMarkdownStore._render_block(unit, "hello", "project_memory")
    assert block == "# \nhello\n\n"


def test_render_block_uses_team_coord_for_daily_files() -> None:
    unit = _unit("u1", "hello", {COORDS_KEY: {"team": "infra"}})
    block = LocalMarkdownStore._render_block(unit, "hello", "team_memory")
    assert block == "# infra\nhello\n\n"


def test_render_block_falls_back_to_empty_title_when_no_team() -> None:
    # team_memory 无 team 坐标 → 标题空串（不用 unit_id 兜底）
    unit = _unit("u1", "hello", {})
    block = LocalMarkdownStore._render_block(unit, "hello", "team_memory")
    assert block == "# \nhello\n\n"


# -- write 落盘与回填 -------------------------------------------------------- #


def test_write_persists_blocks_and_backfills_md_filename(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "hello", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})

    store.write(SCOPE, [unit])

    assert unit.system_metadata[MD_FILENAME_KEY] == "memory/p1/MEMORY.md"
    # 兜底后的 memory_class 写回
    assert unit.system_metadata[MEMORY_CLASS_KEY] == "project_memory"
    written = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "# \nhello\n\n" in written


def test_write_backfills_default_class_when_missing(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "hello", {})  # 无 memory_class / coords

    store.write(SCOPE, [unit])

    assert unit.system_metadata[MEMORY_CLASS_KEY] == "team_memory"
    today = datetime.date.today().isoformat()
    assert unit.system_metadata[MD_FILENAME_KEY] == f"memory/daily_memory/{today}.md"


def test_write_groups_same_file_units_into_one_append(tmp_path) -> None:
    store = _store(tmp_path)
    units = [
        _unit("u1", "first", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
        _unit("u2", "second", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
    ]

    store.write(SCOPE, units)

    text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "# \nfirst\n\n" in text
    assert "# \nsecond\n\n" in text


# -- replace_content --------------------------------------------------------- #


def test_replace_content_replaces_matching_block(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "old", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})
    store.write(SCOPE, [unit])

    replaced = store.replace_content(SCOPE, "memory/p1/MEMORY.md", "old", "new")

    assert replaced is True
    text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "# \nnew\n\n" in text
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
    store.write(
        SCOPE,
        [
            _unit(
                "u1", "keep", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}
            ),
            _unit(
                "u2", "drop", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}
            ),
        ],
    )

    removed = store.remove_content(SCOPE, "memory/p1/MEMORY.md", "drop")

    assert removed is True
    text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "drop" not in text
    assert "# \nkeep\n\n" in text


def test_remove_content_returns_false_when_no_match(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        SCOPE,
        [_unit("u1", "hello", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})],
    )

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


# -- 路径安全（_safe_abs_path 收口）------------------------------------------- #


def test_replace_content_rejects_dotdot_escape(tmp_path) -> None:
    """md_filename 含 .. 逃逸 root → ValidationError（md_filename 源于调用方可写
    的 system_metadata，不可信——逃逸是非法输入，fail-closed 而非返 False）。
    """
    from jiuwen_memory.common.errors import ValidationError

    store = _store(tmp_path)
    with pytest.raises(ValidationError):
        store.replace_content(SCOPE, "../escape.md", "a", "b")


def test_remove_content_rejects_dotdot_escape(tmp_path) -> None:
    from jiuwen_memory.common.errors import ValidationError

    store = _store(tmp_path)
    with pytest.raises(ValidationError):
        store.remove_content(SCOPE, "memory/../../escape.md", "a")


def test_replace_content_rejects_absolute_path(tmp_path) -> None:
    """绝对路径整体替换 join 的 root——直接拒绝。

    用 os.path.abspath 产出平台无关的绝对路径（与 remove 侧同款），
    避免硬编码 Windows 盘符在 POSIX 上不被 isabs 识别而漏拒。
    """
    from jiuwen_memory.common.errors import ValidationError

    store = _store(tmp_path)
    with pytest.raises(ValidationError):
        store.replace_content(SCOPE, os.path.abspath("evil.md"), "a", "b")


def test_remove_content_rejects_absolute_path(tmp_path) -> None:
    from jiuwen_memory.common.errors import ValidationError

    store = _store(tmp_path)
    with pytest.raises(ValidationError):
        store.remove_content(SCOPE, os.path.abspath("evil.md"), "a")


def test_write_rejects_project_coord_escape(tmp_path) -> None:
    """write 的第三条注入路径：coords.project 拼进 md 路径——../../evil 同样穿透，
    _safe_abs_path 收口后抛 ValidationError，不落盘任何文件。
    """
    from jiuwen_memory.common.errors import ValidationError

    store = _store(tmp_path)
    unit = _unit(
        "u1",
        "evil",
        {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "../../evil"}},
    )

    with pytest.raises(ValidationError):
        store.write(SCOPE, [unit])
    # 未在 root 外产生任何文件（root 下也没有——写入前即被拒）。
    assert not (tmp_path / "evil.md").exists()
    assert not (tmp_path.parent / "evil.md").exists()


def test_safe_abs_path_rejects_symlink_escape(tmp_path) -> None:
    """symlink 指向 root 外的文件——realpath 解析出真实位置后同样被拒。"""
    import os as _os

    from jiuwen_memory.common.errors import ValidationError

    root = tmp_path / "md_root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# target\n", encoding="utf-8")
    link = root / "memory" / "link.md"
    link.parent.mkdir(parents=True)
    try:
        _os.symlink(outside, link)
    except OSError as exc:
        if _os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows host lacks the privilege required to create symlinks")
        raise

    store = LocalMarkdownStore(root=str(root))
    with pytest.raises(ValidationError):
        store._safe_abs_path(str(root), "memory/link.md")


def test_safe_abs_path_allows_normal_relative_path(tmp_path) -> None:
    """正常相对路径不受影响：返回 root 内的规范化绝对路径。"""
    store = _store(tmp_path)
    resolved = store._safe_abs_path(str(tmp_path), "memory/p1/MEMORY.md")
    assert resolved == os.path.realpath(str(tmp_path / "memory" / "p1" / "MEMORY.md"))
    assert resolved.startswith(os.path.realpath(str(tmp_path)))


# -- read_content（F08 §D1）-------------------------------------------------- #


def test_resolve_md_path_maps_basename_and_project() -> None:
    """``_resolve_md_path``（ABC 静态法）：basename + project → 相对路径。

    与 ``_md_path`` 同口径。
    """
    from jiuwen_memory.storage.markdown import MarkdownStore

    assert MarkdownStore._resolve_md_path("USER.md", "p1") == "memory/USER.md"
    assert MarkdownStore._resolve_md_path("USER.md", "") == "memory/USER.md"
    assert MarkdownStore._resolve_md_path("MEMORY.md", "p1") == "memory/p1/MEMORY.md"
    assert MarkdownStore._resolve_md_path("MEMORY.md", "") == "memory/MEMORY.md"
    assert (
        MarkdownStore._resolve_md_path("2026-10-15.md", "p1")
        == "memory/p1/daily_memory/2026-10-15.md"
    )
    assert (
        MarkdownStore._resolve_md_path("2026-10-15.md", "")
        == "memory/daily_memory/2026-10-15.md"
    )


def test_read_content_returns_full_file_when_no_line_range(tmp_path) -> None:
    """``line_range=None`` 返回整文件原文（保行尾换行）。"""
    store = _store(tmp_path)
    store.write(
        SCOPE,
        [_unit("u1", "hello world", {
            MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""},
        })],
    )
    text = store.read_content(SCOPE, "MEMORY.md", project="")
    assert "hello world" in text, f"content not found: {text!r}"


def test_read_content_slices_by_line_range_1_based_closed(tmp_path) -> None:
    """``line_range=(start, end)`` 1-based 闭区间切片。"""
    store = _store(tmp_path)
    store.write(
        SCOPE,
        [_unit("u1", "line A", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""}})],
    )
    store.write(
        SCOPE,
        [_unit("u2", "line B", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""}})],
    )
    # memory/MEMORY.md 内容：`# \nline A\n\n# \nline B\n\n`（标题行 + 正文 + 块分隔）
    text = store.read_content(SCOPE, "MEMORY.md", project="", line_range=(1, 2))
    lines = text.splitlines(keepends=True)
    assert len(lines) == 2, f"expect 2 lines in slice, got {len(lines)}: {lines!r}"


def test_read_content_clamps_out_of_range(tmp_path) -> None:
    """越界 clamp 到文件实际行数，超界返回空串。"""
    store = _store(tmp_path)
    store.write(
        SCOPE,
        [_unit("u1", "only line", {
            MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""},
        })],
    )
    # start 远超文件行数 → lo >= hi → 空串
    text = store.read_content(SCOPE, "MEMORY.md", project="", line_range=(999, 1000))
    assert text == "", f"expect empty for out-of-range start, got {text!r}"


def test_read_content_returns_empty_when_file_missing(tmp_path) -> None:
    """文件不存在返回空串（容错，不抛 NotFoundError）。"""
    store = _store(tmp_path)
    text = store.read_content(SCOPE, "MISSING.md", project="")
    assert text == ""


def test_read_content_rejects_path_escape(tmp_path) -> None:
    """basename 逃逸 ``..`` 足够深（逃出 root）由 ``_safe_abs_path`` 拒绝。

    ``_resolve_md_path`` 会拼出 ``memory/daily_memory/<basename>``，basename 的 ``..``
    若层数足够会逃出 root——``_safe_abs_path`` 的 realpath + commonpath 收口拒绝。
    层数不足（``../../etc`` 经 daily_memory 消化回 root 内）不逃逸，是预期边界。
    """
    from jiuwen_memory.common.errors import ValidationError

    store = _store(tmp_path)
    # daily_memory/ 前缀吃掉两层 ..，需足够深才逃出 root（tmp_path 上一层之外）
    with pytest.raises(ValidationError):
        store.read_content(SCOPE, "../../../../../etc/passwd", project="")


# -- edit_content（F08 §D5 / R5）-------------------------------------------- #


def test_edit_content_replaces_unique_substring(tmp_path) -> None:
    """唯一 old_text 精确替换一处 + 原子写。"""
    store = _store(tmp_path)
    store.write(
        SCOPE,
        [_unit("u1", "hello world", {
            MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""},
        })],
    )
    store.edit_content(SCOPE, "MEMORY.md", "hello", "goodbye", project="")
    text = store.read_content(SCOPE, "MEMORY.md", project="")
    assert "goodbye" in text, f"replacement not written: {text!r}"
    assert "hello world" not in text, f"old content still present: {text!r}"


def test_edit_content_accepts_cross_block_text(tmp_path) -> None:
    """old_text 任意文本（跨块均可），不强制对齐块边界。"""
    store = _store(tmp_path)
    meta = {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""}}
    store.write(SCOPE, [_unit("u1", "alpha", meta)])
    store.write(SCOPE, [_unit("u2", "beta", meta)])
    # 跨块子串（含块间 ``\n\n`` + 标题行）
    old = "alpha\n\n# \nbeta"
    # 确认 old 在文件中唯一存在
    full = store.read_content(SCOPE, "MEMORY.md", project="")
    assert full.count(old) == 1, f"old_text not unique or missing: {full!r}"
    store.edit_content(SCOPE, "MEMORY.md", old, "merged", project="")
    new_full = store.read_content(SCOPE, "MEMORY.md", project="")
    assert "merged" in new_full, f"cross-block replace failed: {new_full!r}"
    assert old not in new_full, f"old cross-block text still present: {new_full!r}"


def test_edit_content_raises_not_found_when_old_text_absent(tmp_path) -> None:
    """old_text 不存在（count==0）抛 NotFoundError。"""
    from jiuwen_memory.common.errors import NotFoundError

    store = _store(tmp_path)
    store.write(
        SCOPE,
        [_unit("u1", "hello", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""}})],
    )
    with pytest.raises(NotFoundError):
        store.edit_content(SCOPE, "MEMORY.md", "nonexistent", "x", project="")


def test_edit_content_raises_validation_when_old_text_not_unique(tmp_path) -> None:
    """old_text 不唯一（count>1）抛 ValidationError。"""
    from jiuwen_memory.common.errors import ValidationError

    store = _store(tmp_path)
    meta = {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": ""}}
    store.write(SCOPE, [_unit("u1", "dup", meta)])
    store.write(SCOPE, [_unit("u2", "dup", meta)])
    with pytest.raises(ValidationError):
        store.edit_content(SCOPE, "MEMORY.md", "dup", "unique", project="")


def test_edit_content_raises_not_found_when_file_missing(tmp_path) -> None:
    """文件不存在抛 NotFoundError（edit 不容错，与 read 区分）。"""
    from jiuwen_memory.common.errors import NotFoundError

    store = _store(tmp_path)
    with pytest.raises(NotFoundError):
        store.edit_content(SCOPE, "MISSING.md", "x", "y", project="")


def test_edit_content_resolves_project_subdir(tmp_path) -> None:
    """project 非空时解析到 ``memory/{project}/MEMORY.md`` 子目录。"""
    store = _store(tmp_path)
    store.write(
        SCOPE,
        [_unit("u1", "project content", {
            MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"},
        })],
    )
    store.edit_content(SCOPE, "MEMORY.md", "project content", "edited", project="p1")
    text = store.read_content(SCOPE, "MEMORY.md", project="p1")
    assert "edited" in text, f"project-scoped edit failed: {text!r}"
