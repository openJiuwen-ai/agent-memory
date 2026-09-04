"""CompositeStorage 领域接口、能力与安全边界。"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.errors import (
    PermissionDeniedError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageAction, StorageSecurity
from jiuwen_memory.storage.storage import StorageCapability, StorageProducer
from jiuwen_memory.storage.storage_impl import CompositeStorage
from jiuwen_memory.storage.types import IndexRemoveMode, KVMemoryListResult

pytestmark = pytest.mark.unit


class DenyWritesSecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if action in {StorageAction.ADD, StorageAction.UPDATE, StorageAction.DELETE}:
            raise PermissionDeniedError(action.value)


class RecordingKVStore(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.list_extensions: dict[str, str] | None = None
        self.mget_batches: list[list[str]] = []

    def list(self, scope: Scope, **kwargs: Any) -> KVMemoryListResult:
        self.list_extensions = kwargs.get("extensions")
        return super().list(scope, **kwargs)

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        self.mget_batches.append(list(keys))
        return super().mget(scope, keys)


def _unit(scope: Scope, unit_id: str, content: str = "content") -> MemoryUnit:
    return MemoryUnit(id=unit_id, scope=scope, segments=[Segment(content=content)])


def test_capabilities_and_ports_have_one_source_of_truth() -> None:
    kv = InMemoryKVStore()
    storage = CompositeStorage(kv=kv)

    assert storage.capabilities() == frozenset({StorageCapability.KV})
    assert storage.has_kv()
    assert not storage.has_vector()
    assert storage.kv.store_type() == kv.store_type()
    assert not storage.kv.security.enabled()
    with pytest.raises(UnsupportedStorageCapabilityError):
        _ = storage.vector


def test_memory_unit_crud_and_list_preserve_scope_and_count() -> None:
    scope = Scope(org="org", space="space", user="user")
    kv = RecordingKVStore()
    storage = CompositeStorage(kv=kv)
    first = _unit(scope, "u1", "first")
    second = _unit(scope, "u2", "second")

    storage.add(scope, [first, second])
    assert [unit.id for unit in storage.get(scope, ["u2", "missing", "u1"])] == ["u2", "u1"]
    assert [unit.id for unit in storage.get(scope, ["u1", "u1"])] == ["u1", "u1"]

    page = storage.list(scope, offset=1, limit=1, extensions={"route": "custom"})
    assert page.count == 2
    assert len(page.items) == 1
    assert kv.list_extensions == {"route": "custom"}

    updated = _unit(scope, "u1", "updated")
    storage.update(scope, [updated])
    assert storage.get(scope, ["u1"])[0].content == "updated"

    storage.delete(scope, ["u1"])
    assert storage.get(scope, ["u1"]) == []


def test_soft_delete_is_noop_and_body_stays_readable() -> None:
    """SOFT 软删除：无检索索引可移除，CompositeStorage 空操作，本体 get/list 仍可读。"""
    scope = Scope(org="org", space="space", user="user")
    storage = CompositeStorage(kv=RecordingKVStore())
    unit = _unit(scope, "u1", "first")
    storage.add(scope, [unit])

    storage.delete(scope, ["u1"], mode=IndexRemoveMode.SOFT)

    assert storage.get(scope, ["u1"]) == [unit]
    assert storage.list(scope).count == 1

    storage.delete(scope, ["u1"], mode=IndexRemoveMode.HARD)
    assert storage.get(scope, ["u1"]) == []


def test_get_reads_truth_source_in_one_deduplicated_batch() -> None:
    scope = Scope(org="org")
    kv = RecordingKVStore()
    storage = CompositeStorage(kv=kv)
    storage.add(scope, [_unit(scope, "u1"), _unit(scope, "u2")])

    # 一次 mget 覆盖去重后的 key；返回按输入顺序展开，重复 id 各自返回。
    assert [unit.id for unit in storage.get(scope, ["u2", "u1", "u2"])] == ["u2", "u1", "u2"]
    assert kv.mget_batches == [[memory_key("u2"), memory_key("u1")]]

    # mget 任一 key 缺失即抛 NotFoundError，由 _get_units 回退逐条并跳过缺失。
    kv.mget_batches.clear()
    assert [unit.id for unit in storage.get(scope, ["u1", "missing"])] == ["u1"]
    assert kv.mget_batches == [[memory_key("u1"), memory_key("missing")]]


def test_add_rejects_unit_owned_by_another_scope() -> None:
    requested = Scope(org="org", space="one")
    other = Scope(org="org", space="two")
    storage = CompositeStorage(kv=InMemoryKVStore())

    with pytest.raises(ValidationError):
        storage.add(requested, [_unit(other, "u1")])


def test_common_security_guards_domain_and_direct_port_operations() -> None:
    scope = Scope(org="org")
    storage = CompositeStorage(kv=InMemoryKVStore(), security=DenyWritesSecurity())

    with pytest.raises(PermissionDeniedError):
        storage.add(scope, [_unit(scope, "u1")])
    with pytest.raises(PermissionDeniedError):
        storage.kv.insert(scope, "/raw", b"value")

    assert storage.get(scope, ["missing"]) == []


def test_health_checks_storage_security_and_declared_store() -> None:
    storage = CompositeStorage(kv=InMemoryKVStore())

    assert storage.health() is None


def test_storage_producer_builds_named_composite_with_configured_ports() -> None:
    register_backends()
    context = AssemblyContext.from_dict(
        {
            "kv_store": {"truth": "memory"},
            "vector_store": {"semantic": "memory"},
            "storage": {
                "main": {
                    "target": "composite",
                    "params": {"kv_store": "truth", "vector_store": "semantic"},
                }
            },
        }
    )

    storage = StorageProducer.build_named("main", context)

    assert isinstance(storage, CompositeStorage)
    assert storage.capabilities() == frozenset(
        {StorageCapability.KV, StorageCapability.VECTOR}
    )
    assert StorageProducer.build_named("main", context) is storage


def test_storage_producer_rejects_unknown_retrieval_pipeline() -> None:
    register_backends()

    with pytest.raises(ValidationError, match="preferred_retrieval_pipeline"):
        StorageProducer.build(
            "composite",
            {"preferred_retrieval_pipeline": "unknown"},
            AssemblyContext(),
        )


# -- 文档路径（write_document=True） ---------------------------------------- #
# 文档模式真源 = md 人类视图 + SQLite 影子索引，KV 不参与（F07 §3.1 互斥路径）。
# 用真实 LocalMarkdownStore + SqliteDocumentShadowIndex（降级模式，无 embedder）
# 验证 add/update/delete/get/list 的分流，不 mock 算子——md 落盘与影子索引三表
# 是文档记忆的核心契约。

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import COORDS_KEY, MD_FILENAME_KEY, MEMORY_CLASS_KEY
from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex


def _doc_storage(tmp_path) -> CompositeStorage:
    return CompositeStorage(
        markdown=LocalMarkdownStore(root=str(tmp_path)),
        shadow_index=SqliteDocumentShadowIndex(
            db_path=str(tmp_path / "shadow.db"), tokenizer=WhitespaceTokenizer()
        ),
        write_document=True,
    )


def _doc_unit(scope: Scope, unit_id: str, content: str, project: str = "p1") -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=scope,
        segments=[Segment(content=content)],
        system_metadata={
            MEMORY_CLASS_KEY: "project_memory",
            COORDS_KEY: {"project": project},
        },
    )


def test_write_document_flag_is_fixed_at_assembly(tmp_path) -> None:
    assert CompositeStorage(kv=InMemoryKVStore()).should_write_document() is False
    assert _doc_storage(tmp_path).should_write_document() is True


def test_sanitize_document_content_folds_multiline_to_single_line() -> None:
    unit = MemoryUnit(
        id="u1", scope=Scope(org="org"), segments=[Segment(content="line one\nline two\n\nthree")]
    )
    CompositeStorage._sanitize_document_content([unit])
    assert unit.segments[0].content == "line one line two three"


def test_sanitize_document_content_leaves_single_line_untouched() -> None:
    unit = MemoryUnit(id="u1", scope=Scope(org="org"), segments=[Segment(content="no newline")])
    CompositeStorage._sanitize_document_content([unit])
    assert unit.segments[0].content == "no newline"


def test_sanitize_document_content_skips_empty_segments() -> None:
    unit = MemoryUnit(id="u1", scope=Scope(org="org"), segments=[])
    CompositeStorage._sanitize_document_content([unit])  # 不抛


def test_document_mode_add_writes_md_and_shadow_not_kv(tmp_path) -> None:
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "deploy cluster")])

    # 影子索引真源可读（无 kv 端口，get 走 shadow 不碰 KV）。
    got = storage.get(scope, ["u1"])
    assert [u.id for u in got] == ["u1"]
    assert got[0].segments[0].content == "deploy cluster"
    # md 人类视图落盘。
    md = tmp_path / "memory" / "p1" / "MEMORY.md"
    assert md.exists()
    assert "deploy cluster" in md.read_text(encoding="utf-8")


def test_document_mode_add_folds_multiline_content(tmp_path) -> None:
    """文档路径入口把多行 content 折叠单行，md/索引/后续 replace 锚四方一致。"""
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "line one\nline two")])

    assert storage.get(scope, ["u1"])[0].segments[0].content == "line one line two"
    md = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "line one line two" in md
    assert "\nline two" not in md


def test_document_mode_get_and_list(tmp_path) -> None:
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "first"), _doc_unit(scope, "u2", "second")])

    assert [u.id for u in storage.get(scope, ["u2", "missing", "u1"])] == ["u2", "u1"]
    page = storage.list(scope)
    assert page.count == 2
    assert {u.id for u in page.items} == {"u1", "u2"}


def test_document_mode_update_replaces_md_block(tmp_path) -> None:
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "old content")])

    (old,) = storage.get(scope, ["u1"])
    old.segments[0].content = "new content"
    storage.update(scope, [old])

    assert storage.get(scope, ["u1"])[0].segments[0].content == "new content"
    md = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "new content" in md
    assert "old content" not in md


def test_document_mode_delete_removes_md_block(tmp_path) -> None:
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "gone content")])

    storage.delete(scope, ["u1"])

    assert storage.get(scope, ["u1"]) == []
    md = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "gone content" not in md
