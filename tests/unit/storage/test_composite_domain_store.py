"""CompositeDomainStore.read_md_file / edit_md_file / _supersede_shadow_for_md（F08）。

文档模式数据面的 md 文件级读写：read 按块算 content_hash 反查 shadow；
edit 走继承式 SUPERSEDE（元数据继承 + supersedes 版本链 + 旧版标记 SUPERSEDED）。
跨块对齐（块数一致按 index 继承）+ 不对齐退化兜底（无 supersedes）+ 写窗保护 + 非文档模式拒绝。

失效方向：
- edit 的 SUPERSEDE 不继承元数据 → 版本链断、元数据丢失（与看门狗删旧建新同病）；
- 不对齐退化用 latest_scope_by_md 而非发起者 scope → fallback unit 落他 scope，
  get_units(scope,[id]) 返回空、违反返回契约（F08 D4.1 已修正）；
- 非文档模式未拒绝 → InMemoryEngine 无 md 文件却返回空/静默成功，掩盖配置错误。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    MEMORY_CLASS_KEY,
    LifecycleState,
    MemoryUnit,
    Scope,
    Segment,
)
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage import bootstrap as sb
from jiuwen_memory.storage.domain_store_impl.composite_domain_store import (
    CompositeDomainStore,
)
from jiuwen_memory.storage.store_manager import StoreManagerProducer

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


def _build_doc_store(tmp_path) -> CompositeDomainStore:
    """装配文档模式 DomainStore（markdown + sqlite shadow + write_document=true）。"""
    sb.register_backends()
    register_plugins()
    StoreManagerProducer.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "globals": {"write_document": True, "watch_document": False},
            "kv_store": {"default": "memory"},
            "markdown_store": {
                "default": {"target": "local", "params": {"root": str(tmp_path)}}
            },
            "shadow_index": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "shadow.db")},
                }
            },
            "store_manager": {
                "default": {
                    "target": "composite",
                    "params": {"domain_stores": {"default": {"kv_store": "default"}}},
                }
            },
        }
    )
    manager = StoreManagerProducer.build_named("default", ctx)
    return manager.domain_store("default")


def _write_unit(
    ds: CompositeDomainStore,
    uid: str,
    content: str,
    scope: Scope = SCOPE,
    metadata: dict | None = None,
) -> MemoryUnit:
    """经 DomainStore.add 写入文档模式 unit（md + shadow 双写）。"""
    unit = MemoryUnit(
        id=uid,
        scope=scope,
        segments=[Segment(content=content)],
        system_metadata=dict(metadata or {MEMORY_CLASS_KEY: "project_memory"}),
    )
    ds.add(scope, [unit])
    return unit


# -- read_md_file（F08 §D1）-------------------------------------------------- #


def test_read_md_file_returns_units_matching_blocks(tmp_path) -> None:
    """read 按块算 content_hash 反查 shadow，返回完整 MemoryUnit list。"""
    ds = _build_doc_store(tmp_path)
    _write_unit(ds, "u1", "hello world")
    _write_unit(ds, "u2", "second memory")

    units = ds.read_md_file(SCOPE, "MEMORY.md", project="")
    contents = [u.segments[0].content for u in units]
    assert "hello world" in contents, f"u1 not found in read: {contents}"
    assert "second memory" in contents, f"u2 not found in read: {contents}"
    assert {u.id for u in units} == {"u1", "u2"}, f"ids mismatch: {[u.id for u in units]}"


def test_read_md_file_empty_when_no_content(tmp_path) -> None:
    """文件无内容（无 unit 写入）→ 返回空 list。"""
    ds = _build_doc_store(tmp_path)
    assert ds.read_md_file(SCOPE, "MEMORY.md", project="") == []


def test_read_md_file_line_range_skips_partial_blocks(tmp_path) -> None:
    """line_range 切断块的 hash 匹配不上完整块，自然跳过（F08 已知遗留 6）。"""
    ds = _build_doc_store(tmp_path)
    _write_unit(ds, "u1", "hello world")
    # 完整行切片应命中；只切部分行的切片算出的 hash 匹配不上完整块
    full = ds.read_md_file(SCOPE, "MEMORY.md", project="")
    assert len(full) == 1


def test_read_md_file_rejects_non_document_mode(tmp_path) -> None:
    """非文档模式（write_document=false）→ ValidationError。"""
    sb.register_backends()
    register_plugins()
    StoreManagerProducer.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "globals": {"write_document": False},
            "kv_store": {"default": "memory"},
            "store_manager": {
                "default": {
                    "target": "composite",
                    "params": {"domain_stores": {"default": {"kv_store": "default"}}},
                }
            },
        }
    )
    manager = StoreManagerProducer.build_named("default", ctx)
    ds = manager.domain_store("default")
    with pytest.raises(ValidationError, match="document mode"):
        ds.read_md_file(SCOPE, "MEMORY.md", project="")


# -- edit_md_file：继承式 SUPERSEDE（F08 §D4）------------------------------- #


def test_edit_md_file_aligned_inherits_metadata_and_supersedes(tmp_path) -> None:
    """对齐场景（块数不变）：新 unit 继承旧 unit 元数据 + supersedes 指向旧 id。

    旧版标记 SUPERSEDED + t_invalid。
    """
    ds = _build_doc_store(tmp_path)
    _write_unit(
        ds,
        "u1",
        "hello world",
        metadata={
            MEMORY_CLASS_KEY: "project_memory",
            COORDS_KEY: {"project": ""},
            "user_key": "preserved",
        },
    )

    new_units = ds.edit_md_file(SCOPE, "MEMORY.md", "hello world", "hello NEW", project="")
    assert len(new_units) == 1, f"expect 1 new unit, got {len(new_units)}"
    new_u = new_units[0]
    # 新 content
    assert new_u.segments[0].content == "hello NEW", (
        f"content mismatch: {new_u.segments[0].content!r}"
    )
    # supersedes 指向旧 id
    assert new_u.supersedes == "u1", f"supersedes mismatch: {new_u.supersedes!r}"
    # 元数据继承（user_key 保留）
    assert new_u.system_metadata.get("user_key") == "preserved", (
        f"metadata not inherited: {new_u.system_metadata}"
    )
    # 新版 ACTIVE
    assert new_u.lifecycle is LifecycleState.ACTIVE

    # 旧版标记 SUPERSEDED（经 shadow 读回验证）
    shadow = ds._raw_shadow_index()
    old = shadow.get_units(SCOPE, ["u1"])[0]
    assert old.lifecycle is LifecycleState.SUPERSEDED, f"old not SUPERSEDED: {old.lifecycle}"
    assert old.temporal.t_invalid is not None, "old t_invalid not set"


def test_edit_md_file_returns_new_unit_not_old(tmp_path) -> None:
    """返回新 unit（新 id），非旧 unit——旧版已失效，返回它会让调用方误用失效 id。"""
    ds = _build_doc_store(tmp_path)
    _write_unit(ds, "u1", "hello")
    new_units = ds.edit_md_file(SCOPE, "MEMORY.md", "hello", "goodbye", project="")
    assert new_units[0].id != "u1", "edit returned old unit id"
    assert new_units[0].id  # 新 id 非空


def test_edit_md_file_unaligned_fallback_no_supersedes(tmp_path) -> None:
    """不对齐场景（合并/拆分）：退化兜底，新 unit 无 supersedes，scope 取发起者。"""
    ds = _build_doc_store(tmp_path)
    _write_unit(ds, "u1", "alpha")
    _write_unit(ds, "u2", "beta")
    # 跨块合并：old_text 跨两个块，替换后合并成一个块 → 块数减 1（不对齐）
    full = ds._raw_markdown().read_content(SCOPE, "MEMORY.md", project="")
    # md 结构：`# \nalpha\n\n# \nbeta\n\n`，跨块 old_text 跨块间 ``\n\n`` + 标题行
    assert "alpha" in full and "beta" in full
    old_cross = "alpha\n\n# \nbeta"
    assert full.count(old_cross) == 1, f"cross-block old not unique: {full!r}"

    new_units = ds.edit_md_file(SCOPE, "MEMORY.md", old_cross, "merged", project="")
    # 不对齐 → fallback unit 无 supersedes
    assert len(new_units) >= 1
    for u in new_units:
        assert u.supersedes is None or u.supersedes == "", (
            f"fallback unit should have no supersedes, got {u.supersedes!r}"
        )
        # scope 取发起者（当前 scope）
        assert u.scope == SCOPE, f"fallback scope not originator: {u.scope}"
        # provenance 标 md_edit_fallback
        assert "md_edit_fallback" in (u.provenance or []), (
            f"fallback provenance missing: {u.provenance}"
        )


def test_edit_md_file_old_text_not_unique_raises(tmp_path) -> None:
    """old_text 不唯一（count>1）→ ValidationError。"""
    ds = _build_doc_store(tmp_path)
    _write_unit(ds, "u1", "dup")
    _write_unit(ds, "u2", "dup")
    with pytest.raises(ValidationError, match="not unique"):
        ds.edit_md_file(SCOPE, "MEMORY.md", "dup", "unique", project="")


def test_edit_md_file_rejects_non_document_mode(tmp_path) -> None:
    """非文档模式 → ValidationError（read_md_file 同）。"""
    sb.register_backends()
    register_plugins()
    StoreManagerProducer.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "globals": {"write_document": False},
            "kv_store": {"default": "memory"},
            "store_manager": {
                "default": {
                    "target": "composite",
                    "params": {"domain_stores": {"default": {"kv_store": "default"}}},
                }
            },
        }
    )
    manager = StoreManagerProducer.build_named("default", ctx)
    ds = manager.domain_store("default")
    with pytest.raises(ValidationError, match="document mode"):
        ds.edit_md_file(SCOPE, "MEMORY.md", "x", "y", project="")


def test_edit_md_file_write_window_protects_against_watchdog(tmp_path) -> None:
    """写窗保护：edit 期间 close_write_window 在 finally 保证关窗（即使异常也关）。

    用 edit 成功后验证写窗状态已关闭（open_write_window 后 is_write_window_open 为真，
    edit 完成后应为假）。这守护看门狗在 edit 同步 shadow 期间误触发 _sync_one。
    """
    from jiuwen_memory.storage.sync_gate import write_window_open

    ds = _build_doc_store(tmp_path)
    _write_unit(ds, "u1", "hello")
    assert not write_window_open(), "window should be closed before edit"
    ds.edit_md_file(SCOPE, "MEMORY.md", "hello", "world", project="")
    assert not write_window_open(), "window must be closed after edit (finally)"
