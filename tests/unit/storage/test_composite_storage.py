"""CompositeStoreManager 端口/能力/安全 + CompositeDomainStore 领域接口。"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为

from typing import Any

import pytest

from jiuwen_memory.common.errors import (
    PermissionDeniedError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import MemoryUnit, RetrievalPipeline, Scope, Segment, memory_key
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageAction, StorageSecurity
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManagerProducer
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import IndexRemoveMode, KVMemoryListResult
from tests.conftest import make_storage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """具名实例缓存跨测试隔离：producer 级 build_named 用例依赖干净缓存。"""
    Factory.reset_all()
    yield
    Factory.reset_all()


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
    storage = make_storage(kv=kv)

    assert storage.capabilities() == frozenset({StorageCapability.KV})
    assert storage.has_kv()
    assert not storage.has_vector()
    assert storage.kv().store_type() == kv.store_type()
    assert not storage.kv().security.enabled()
    with pytest.raises(UnsupportedStorageCapabilityError):
        storage.vector()


def test_memory_unit_crud_and_list_preserve_scope_and_count() -> None:
    scope = Scope(org="org", space="space", user="user")
    kv = RecordingKVStore()
    domain_store = make_storage(kv=kv).domain_store()
    first = _unit(scope, "u1", "first")
    second = _unit(scope, "u2", "second")

    domain_store.add(scope, [first, second])
    assert [unit.id for unit in domain_store.get(scope, ["u2", "missing", "u1"])] == [
        "u2",
        "u1",
    ]
    assert [unit.id for unit in domain_store.get(scope, ["u1", "u1"])] == ["u1", "u1"]

    page = domain_store.list(scope, offset=1, limit=1, extensions={"route": "custom"})
    assert page.count == 2
    assert len(page.items) == 1
    assert kv.list_extensions == {"route": "custom"}

    updated = _unit(scope, "u1", "updated")
    domain_store.update(scope, [updated])
    assert domain_store.get(scope, ["u1"])[0].content == "updated"

    domain_store.delete(scope, ["u1"])
    assert domain_store.get(scope, ["u1"]) == []


def test_soft_delete_is_noop_and_body_stays_readable() -> None:
    """SOFT 软删除：无检索索引可移除，CompositeDomainStore 空操作，本体仍可读。"""
    scope = Scope(org="org", space="space", user="user")
    domain_store = make_storage(kv=RecordingKVStore()).domain_store()
    unit = _unit(scope, "u1", "first")
    domain_store.add(scope, [unit])

    domain_store.delete(scope, ["u1"], mode=IndexRemoveMode.SOFT)

    assert domain_store.get(scope, ["u1"]) == [unit]
    assert domain_store.list(scope).count == 1

    domain_store.delete(scope, ["u1"], mode=IndexRemoveMode.HARD)
    assert domain_store.get(scope, ["u1"]) == []


def test_get_reads_truth_source_in_one_deduplicated_batch() -> None:
    scope = Scope(org="org")
    kv = RecordingKVStore()
    domain_store = make_storage(kv=kv).domain_store()
    domain_store.add(scope, [_unit(scope, "u1"), _unit(scope, "u2")])

    # 一次 mget 覆盖去重后的 key；返回按输入顺序展开，重复 id 各自返回。
    assert [unit.id for unit in domain_store.get(scope, ["u2", "u1", "u2"])] == [
        "u2",
        "u1",
        "u2",
    ]
    assert kv.mget_batches == [[memory_key("u2"), memory_key("u1")]]

    # mget 任一 key 缺失即抛 NotFoundError，由 _get_units 回退逐条并跳过缺失。
    kv.mget_batches.clear()
    assert [unit.id for unit in domain_store.get(scope, ["u1", "missing"])] == ["u1"]
    assert kv.mget_batches == [[memory_key("u1"), memory_key("missing")]]


def test_add_rejects_unit_owned_by_another_scope() -> None:
    requested = Scope(org="org", space="one")
    other = Scope(org="org", space="two")
    domain_store = make_storage(kv=InMemoryKVStore()).domain_store()

    with pytest.raises(ValidationError):
        domain_store.add(requested, [_unit(other, "u1")])


def test_common_security_guards_domain_and_direct_port_operations() -> None:
    scope = Scope(org="org")
    storage = make_storage(kv=InMemoryKVStore(), security=DenyWritesSecurity())
    domain_store = storage.domain_store()

    with pytest.raises(PermissionDeniedError):
        domain_store.add(scope, [_unit(scope, "u1")])
    with pytest.raises(PermissionDeniedError):
        storage.kv().insert(scope, "/raw", b"value")

    assert domain_store.get(scope, ["missing"]) == []


def test_health_checks_storage_security_and_declared_store() -> None:
    storage = make_storage(kv=InMemoryKVStore())

    assert storage.health() is None


def test_store_manager_producer_builds_named_composite_with_configured_ports() -> None:
    register_backends()
    context = AssemblyContext.from_dict(
        {
            # 具名 manager 的召回路装配经 globals.store_manager 指名回取本实例
            #（预注册缓存命中）；关 graph 避免无 graph 端口时装配失败。
            "globals": {"store_manager": "main", "graph_enabled": False},
            "kv_store": {"truth": "memory"},
            "vector_store": {"semantic": "memory"},
            "store_manager": {"main": {"target": "composite"}},
        }
    )

    storage = StoreManagerProducer.build_named("main", context)

    assert isinstance(storage, CompositeStoreManager)
    # 命名空间实例全量成为端口，端口名即实例名；capability 由端口表推导。
    assert storage.capabilities() == frozenset(
        {StorageCapability.KV, StorageCapability.VECTOR}
    )
    assert storage.has_kv("truth") and storage.has_vector("semantic")
    assert StoreManagerProducer.build_named("main", context) is storage


def test_store_manager_producer_rejects_unknown_retrieval_pipeline() -> None:
    register_backends()

    ctx = AssemblyContext.from_dict({"kv_store": {"default": "memory"}})

    with pytest.raises(ValidationError, match="preferred_retrieval_pipeline"):
        StoreManagerProducer.build(
            "composite",
            {"domain_stores": {"default": {"preferred_retrieval_pipeline": "unknown"}}},
            ctx,
        )


def test_store_manager_shares_named_default_kv_instance() -> None:
    """manager 的 KV 端口背后就是 kv_store.default 具名实例，不匿名新建（防真源分裂）。

    行为验证：对具名实例直接写入，经 manager 端口能读到——绕开 manager 的消费方
    （如 evolver 的 message_store）与 manager 消费方看到的是同一份真源。
    """
    register_backends()
    ctx = AssemblyContext.from_dict({"kv_store": {"default": "memory"}})
    scope = Scope(org="org")

    manager = StoreManagerProducer.build("composite", {}, ctx)
    KvProducer.build_named("default", ctx).insert(scope, "/probe", b"v")

    assert manager.kv().get(scope, "/probe") == b"v"


def test_manager_without_kv_defers_failure_to_domain_methods() -> None:
    """manager 不校验 capability 必需性：无 KV 照常构造，缺真源由数据面调用时报错。

    「哪类存储不可或缺」是消费方的约束而非管理面的——这也让两条入口行为一致
    （``__init__`` 同样允许无 KV，分层索引专用装配即如此）。
    """
    manager = CompositeStoreManager(fulltext=InMemoryFulltextStore(WhitespaceTokenizer()))

    assert StorageCapability.KV not in manager.capabilities()
    assert not manager.has_kv()

    domain_store = CompositeDomainStore.for_manager(manager)
    with pytest.raises(UnsupportedStorageCapabilityError, match="kv"):
        domain_store.list(Scope(org="o", user="u"))


def test_domain_store_uses_named_kv_port_from_config() -> None:
    """数据面真源端口由装配期 resolve_name(ds_config, "kv_store") 指名，不硬编码 default。

    判别式：kv_store 命名空间同时声明 default 与 truth，domain_stores.default 指名 truth。
    数据面写入后只有 truth 端口能读到——若仍走 default 则本断言失败。
    """
    register_backends()
    ctx = AssemblyContext.from_dict(
        {
            "globals": {"store_manager": "main", "vector_enabled": False, "graph_enabled": False},
            "kv_store": {"default": "memory", "truth": "memory"},
            "store_manager": {
                "main": {
                    "target": "composite",
                    "params": {"domain_stores": {"default": {"kv_store": "truth"}}},
                }
            },
        }
    )
    scope = Scope(org="org", user="u")

    manager = StoreManagerProducer.build_named("main", ctx)
    manager.domain_store().add(scope, [_unit(scope, "u1")])

    assert [u.id for u in manager.domain_store().list(scope).items] == ["u1"]
    # 真源落在 truth 端口，default 端口是空的
    assert manager.kv("truth").list(scope).count == 1
    assert manager.kv().list(scope).count == 0


# -- 文档路径（write_document=True） ---------------------------------------- #
# 文档模式真源 = md 人类视图 + SQLite 影子索引，KV 不参与（F07 §3.1 互斥路径）。
# 用真实 LocalMarkdownStore + SqliteDocumentShadowIndex（降级模式，无 embedder）
# 验证 add/update/delete/get/list 的分流，不 mock 算子——md 落盘与影子索引三表
# 是文档记忆的核心契约。

from jiuwen_memory.common.type_def import COORDS_KEY, MD_FILENAME_KEY, MEMORY_CLASS_KEY
from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex


def _doc_storage(tmp_path) -> CompositeDomainStore:
    manager = CompositeStoreManager(
        markdown=LocalMarkdownStore(root=str(tmp_path)),
        shadow_index=SqliteDocumentShadowIndex(
            db_path=str(tmp_path / "shadow.db"), tokenizer=WhitespaceTokenizer()
        ),
    )
    return CompositeDomainStore(
        manager=manager,
        preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK,
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
    plain = CompositeDomainStore(
        manager=CompositeStoreManager(kv=InMemoryKVStore()),
        preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK,
    )
    assert plain.should_write_document() is False
    assert _doc_storage(tmp_path).should_write_document() is True


def test_sanitize_document_content_folds_multiline_to_single_line() -> None:
    unit = MemoryUnit(
        id="u1", scope=Scope(org="org"), segments=[Segment(content="line one\nline two\n\nthree")]
    )
    CompositeDomainStore._sanitize_document_content([unit])
    assert unit.segments[0].content == "line one line two three"


def test_sanitize_document_content_leaves_single_line_untouched() -> None:
    unit = MemoryUnit(id="u1", scope=Scope(org="org"), segments=[Segment(content="no newline")])
    CompositeDomainStore._sanitize_document_content([unit])
    assert unit.segments[0].content == "no newline"


def test_sanitize_document_content_skips_empty_segments() -> None:
    unit = MemoryUnit(id="u1", scope=Scope(org="org"), segments=[])
    CompositeDomainStore._sanitize_document_content([unit])  # 不抛


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


def test_document_mode_update_finds_md_via_old_when_new_unit_lacks_md_filename(tmp_path) -> None:
    """new unit 不带 MD_FILENAME_KEY 时，OVERWRITE 分支须从 old.system_metadata 取 md_filename，
    否则 if md_filename 守卫失败 → md.replace_content 被静默跳过 → md 与 shadow 漂移。

    复现 bug A：composite 用 new unit 取内部路径。修复后从 old 取（对齐 delete 范式）。
    """
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "old content")])

    calls: list[tuple] = []
    orig = LocalMarkdownStore.replace_content

    def spy(self, scope_arg, md_filename, old_content, new_content):
        calls.append((md_filename, old_content, new_content))
        return orig(self, scope_arg, md_filename, old_content, new_content)

    # 读回 old（自带 MD_FILENAME_KEY），构造 new unit：同 id、改 content，但清掉 MD_FILENAME_KEY
    (old,) = storage.get(scope, ["u1"])
    new_meta = {k: v for k, v in (old.system_metadata or {}).items() if k != MD_FILENAME_KEY}
    new_unit = MemoryUnit(
        id=old.id, scope=old.scope, segments=[Segment(content="new content")],
        system_metadata=new_meta,
    )
    LocalMarkdownStore.replace_content = spy
    try:
        storage.update(scope, [new_unit])
    finally:
        LocalMarkdownStore.replace_content = orig

    # md.replace_content 被调用（从 old 取到 md_filename，未被守卫跳过）
    assert calls and calls[0][0] == "memory/p1/MEMORY.md"
    assert calls[0][1] == "old content" and calls[0][2] == "new content"
    md_text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "new content" in md_text
    assert "old content" not in md_text


def test_document_mode_supersede_finds_md_via_old_when_new_unit_lacks_md_filename(tmp_path) -> None:
    """new unit 不带 MD_FILENAME_KEY 时，SUPERSEDE 分支须从 old.system_metadata 取 md_filename，
    否则 md.remove_content 被静默跳过 → md 留幽灵块。

    复现 bug A 的 SUPERSEDE 分支。content 不变、lifecycle ACTIVE→SUPERSEDED。
    """
    from jiuwen_memory.common.type_def.memory import LifecycleState

    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "stable content")])

    calls: list[tuple] = []
    orig = LocalMarkdownStore.remove_content

    def spy(self, scope_arg, md_filename, content):
        calls.append((md_filename, content))
        return orig(self, scope_arg, md_filename, content)

    (old,) = storage.get(scope, ["u1"])
    new_meta = {k: v for k, v in (old.system_metadata or {}).items() if k != MD_FILENAME_KEY}
    new_unit = MemoryUnit(
        id=old.id, scope=old.scope, segments=old.segments,  # content 不变
        system_metadata=new_meta,
        lifecycle=LifecycleState.SUPERSEDED,
    )
    # composite 内部 L281 重新从 shadow 读 old（add 后默认 ACTIVE），SUPERSEDE 判定成立
    LocalMarkdownStore.remove_content = spy
    try:
        storage.update(scope, [new_unit])
    finally:
        LocalMarkdownStore.remove_content = orig

    assert calls and calls[0][0] == "memory/p1/MEMORY.md"
    assert calls[0][1] == "stable content"
    md_text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "stable content" not in md_text


def test_document_mode_delete_removes_md_block(tmp_path) -> None:
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "gone content")])

    storage.delete(scope, ["u1"])

    assert storage.get(scope, ["u1"]) == []
    md = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "gone content" not in md


def test_document_mode_delete_batch_removes_all_md_blocks(tmp_path) -> None:
    """批量删除逐 unit 清 md 块——缩进回归（只清最后一个）会让残留块被看门狗
    当"用户新增"以新 uuid 复活成幽灵 unit。
    """
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "first content"), _doc_unit(scope, "u2", "second content")])

    storage.delete(scope, ["u1", "u2"])

    assert storage.get(scope, ["u1", "u2"]) == []
    md = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "first content" not in md
    assert "second content" not in md


def test_document_mode_delete_missing_id_is_noop(tmp_path) -> None:
    """删不存在的 id 幂等不抛错——olds 为空时 md_filename 未绑定的 NameError 回归。"""
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)

    storage.delete(scope, ["never-existed"])  # 不抛 NameError

    # 已删 id 重复删同样幂等。
    storage.add(scope, [_doc_unit(scope, "u1", "real content")])
    storage.delete(scope, ["u1"])
    storage.delete(scope, ["u1"])  # 不抛
    assert storage.get(scope, ["u1"]) == []
    md = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "real content" not in md


def test_document_mode_soft_delete_exits_retrieval_via_lifecycle(tmp_path) -> None:
    """SOFT 删除契约：文档模式下 delete(SOFT) 本身是 no-op，检索退出由
    「先 transition（update FORWARD_ONLY 同步 lifecycle 投影列）再 remove(SOFT)」
    实现——lifecycle 谓词下推后 FTS 不召回，本体与 md 块保留。

    锁定三重排除机制的第①②环：谓词下推 + 投影列同步。若谓词下推
    （_compile_system_filters）或 update 投影列覆写被改坏，本测试失败。
    """
    from jiuwen_memory.common.type_def import FilterClause, FilterGroup, FilterLogic, FilterOp
    from jiuwen_memory.common.type_def.memory import LifecycleState
    from jiuwen_memory.storage.types import IndexWriteMode, TextQuery

    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "retired content")])

    # 遗忘流第①步：transition = 改 lifecycle + update(FORWARD_ONLY)（对齐
    # KVLifecycleManager.transition / InMemoryEngine.delete 的调用序）。
    (unit,) = storage.get(scope, ["u1"])
    unit.lifecycle = LifecycleState.FORGOTTEN
    storage.update(scope, [unit], mode=IndexWriteMode.FORWARD_ONLY)
    # 遗忘流第②步：remove(SOFT)——文档模式 no-op，不删本体不删 md 块。
    storage.delete(scope, ["u1"], mode=IndexRemoveMode.SOFT)

    # 本体保留（SOFT 契约）：get 可读，md 块仍在。
    assert storage.get(scope, ["u1"]) != []
    md = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "retired content" in md

    # 检索退出（FORGOTTEN 不召回）：filters = project 谓词（否则批 1 落 default
    # 不含 p1，断言空洞）+ lifecycle 谓词（对齐 build_system_filters 当前态产出
    # lifecycle IN ('active')），AND 组合下推。
    shadow = storage._raw_shadow_index()
    filters = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("system_metadata.project", FilterOp.IN, ["p1"]),
            FilterClause("lifecycle", FilterOp.IN, ["active"]),
        ],
    )
    hits = shadow.search_fulltext(
        scope, TextQuery(text="retired content", top_k=10, filters=filters)
    )
    assert not any(h.id == "u1" for h in hits)


def test_document_mode_soft_delete_bare_call_keeps_recall(tmp_path) -> None:
    """裸调 delete(SOFT)（未经 transition）不使 unit 退出检索——文档模式 SOFT 是
    no-op 的现状契约，调用方必须先 lifecycle.transition（见上测试）。防止有人
    以为 SOFT 会删投影而依赖它。
    """
    from jiuwen_memory.common.type_def import FilterClause, FilterOp
    from jiuwen_memory.storage.types import TextQuery

    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "still visible")])

    storage.delete(scope, ["u1"], mode=IndexRemoveMode.SOFT)

    # SOFT no-op：本体可读、带 project 谓词的检索仍命中（FORGOTTEN 排除见上测试）。
    assert storage.get(scope, ["u1"]) != []
    shadow = storage._raw_shadow_index()
    filters = FilterClause("system_metadata.project", FilterOp.IN, ["p1"])
    hits = shadow.search_fulltext(
        scope, TextQuery(text="still visible", top_k=10, filters=filters)
    )
    assert any(h.id == "u1" for h in hits)


# -- 写失败补偿（F08 决策四）：注入 shadow/md 失败，断言补偿回滚 + 原异常抛出 -------


def test_document_mode_add_compensates_md_on_shadow_failure(tmp_path, monkeypatch) -> None:
    """add：shadow.insert_units 抛错 → 反向 md.remove_content 删刚写的块，原异常抛出。"""
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    shadow = storage._raw_shadow_index()
    md = storage._raw_markdown()

    # spy 记录补偿调用并委托真实实现（验证块确被删）。
    remove_calls: list[tuple[str, str]] = []
    real_remove = md.remove_content

    def spy_remove(scope_arg, filename, content):
        remove_calls.append((filename, content))
        return real_remove(scope_arg, filename, content)

    monkeypatch.setattr(md, "remove_content", spy_remove)

    def fail_insert(*a, **k):
        raise RuntimeError("shadow insert failed")

    monkeypatch.setattr(shadow, "insert_units", fail_insert)

    with pytest.raises(RuntimeError, match="shadow insert failed"):
        storage.add(scope, [_doc_unit(scope, "u1", "deploy cluster")])

    # 补偿调了 md.remove_content（md.write 已回填 md_filename + content 现成）。
    assert remove_calls, "add 补偿应反向调 md.remove_content 删刚写的块"
    # md 人类视图无残留块（文件可能被删空或留空，存在则不应含该 content）。
    md_path = tmp_path / "memory" / "p1" / "MEMORY.md"
    if md_path.exists():
        assert "deploy cluster" not in md_path.read_text(encoding="utf-8")
    # shadow 未写入（insert 抛错）。
    assert storage.get(scope, ["u1"]) == []


def test_document_mode_update_compensates_shadow_on_md_failure(tmp_path, monkeypatch) -> None:
    """update：md.replace_content 抛错 → shadow.update_units([old]) 还原，原异常抛出。"""
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "old content")])

    shadow = storage._raw_shadow_index()
    md = storage._raw_markdown()

    # spy 记录 update_units 调用序列（期望：new 一次 + old 还原一次）。
    update_calls: list[list[str]] = []
    real_update = shadow.update_units

    def spy_update(scope_arg, units):
        update_calls.append([u.id for u in units])
        return real_update(scope_arg, units)

    monkeypatch.setattr(shadow, "update_units", spy_update)

    def fail_replace(*a, **k):
        raise RuntimeError("md replace failed")

    monkeypatch.setattr(md, "replace_content", fail_replace)

    (old,) = storage.get(scope, ["u1"])
    old.segments[0].content = "new content"
    with pytest.raises(RuntimeError, match="md replace failed"):
        storage.update(scope, [old])

    # update_units 调了两次：第一次写 new，第二次还原 old。
    assert update_calls == [["u1"], ["u1"]]
    # shadow 还原成 old content（补偿生效）。
    assert storage.get(scope, ["u1"])[0].segments[0].content == "old content"
    # md 仍是 old content（replace_content 抛错未改）。
    md_text = (tmp_path / "memory" / "p1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "old content" in md_text


def test_document_mode_delete_compensates_shadow_on_md_failure(tmp_path, monkeypatch) -> None:
    """delete：md.remove_content 抛错 → shadow.insert_units(olds) 回插，原异常抛出。"""
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "gone content")])

    shadow = storage._raw_shadow_index()
    md = storage._raw_markdown()

    # spy 记录补偿 insert（delete_units 后 id 已释放，insert 不冲突）。
    insert_calls: list[list[str]] = []
    real_insert = shadow.insert_units

    def spy_insert(scope_arg, units):
        insert_calls.append([u.id for u in units])
        return real_insert(scope_arg, units)

    monkeypatch.setattr(shadow, "insert_units", spy_insert)

    def fail_remove(*a, **k):
        raise RuntimeError("md remove failed")

    monkeypatch.setattr(md, "remove_content", fail_remove)

    with pytest.raises(RuntimeError, match="md remove failed"):
        storage.delete(scope, ["u1"])

    # 补偿调了 shadow.insert_units(olds)。
    assert insert_calls == [["u1"]]
    # shadow 回插成功（unit 复活，content 还原）。
    back = storage.get(scope, ["u1"])
    assert back != []
    assert back[0].segments[0].content == "gone content"


def test_document_mode_compensation_failure_does_not_mask_original(tmp_path, monkeypatch) -> None:
    """补偿自身抛错也只记 warning，抛出的仍是原异常（非补偿异常）。"""
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    shadow = storage._raw_shadow_index()
    md = storage._raw_markdown()

    # 补偿路径（md.remove_content）也抛错。
    def fail_compensation(*a, **k):
        raise OSError("compensation failed")

    monkeypatch.setattr(md, "remove_content", fail_compensation)

    # 原失败：shadow.insert_units 抛 RuntimeError。
    def fail_insert(*a, **k):
        raise RuntimeError("shadow insert failed")

    monkeypatch.setattr(shadow, "insert_units", fail_insert)

    with pytest.raises(RuntimeError, match="shadow insert failed") as exc_info:
        storage.add(scope, [_doc_unit(scope, "u1", "deploy cluster")])

    # 抛出的是原异常，不是补偿异常（OSError("compensation failed")）。
    assert "compensation failed" not in str(exc_info.value)


def test_document_mode_write_window_open_during_compensation(tmp_path, monkeypatch) -> None:
    """补偿期间写窗口仍开（挡看门狗观察），finally 后关闭。"""
    from jiuwen_memory.storage.sync_gate import write_window_open

    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    shadow = storage._raw_shadow_index()
    md = storage._raw_markdown()

    observed: dict[str, bool] = {}

    def fail_insert(*a, **k):
        observed["during_insert"] = write_window_open()
        raise RuntimeError("boom")

    monkeypatch.setattr(shadow, "insert_units", fail_insert)

    def sample_compensation(*a, **k):
        observed["during_compensation"] = write_window_open()

    monkeypatch.setattr(md, "remove_content", sample_compensation)

    with pytest.raises(RuntimeError):
        storage.add(scope, [_doc_unit(scope, "u1", "x")])

    # insert_units 抛错时窗口已开（处于 open_write_window 与 close 之间）。
    assert observed["during_insert"] is True
    # 补偿（except 块内调 md.remove_content）期间窗口仍开（finally 尚未执行）。
    assert observed["during_compensation"] is True
    # finally 执行后窗口关闭。
    assert write_window_open() is False


# -- md 原子写恢复（层次一）：写阶段失败时 md 文件回到调用前状态 --------------- #


def test_replace_content_atomic_restore_on_write_failure(tmp_path, monkeypatch) -> None:
    """replace_content 写阶段抛错 → _safe_restore 回写旧全文，md 回到调用前。"""
    import builtins

    from jiuwen_memory.storage.markdown_impl import local_markdown_store as md_module

    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    # 写两条块到同 md 文件（update 的 replace_content 在此文件上操作）。
    storage.add(scope, [_doc_unit(scope, "u1", "first content")])
    storage.add(scope, [_doc_unit(scope, "u2", "second content")])
    md_path = tmp_path / "memory" / "p1" / "MEMORY.md"
    original_text = md_path.read_text(encoding="utf-8")
    assert "first content" in original_text and "second content" in original_text

    # 注入模块级 open：读（"r"）放行，写（"w"）抛 OSError（模块 globals 优先于 builtins）。
    real_open = builtins.open

    def fail_on_write(file, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError("disk write failed")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(md_module, "open", fail_on_write, raising=False)

    (old,) = storage.get(scope, ["u1"])
    old.segments[0].content = "first updated"
    with pytest.raises(OSError, match="disk write failed"):
        storage.update(scope, [old])

    # md 文件回到调用前状态（_safe_restore 回写了 original_text）。
    restored = md_path.read_text(encoding="utf-8")
    assert restored == original_text
    assert "first content" in restored  # 旧 content 还在（未被截断丢失）
    assert "second content" in restored  # 其他块完好


def test_remove_content_atomic_restore_on_write_failure(tmp_path, monkeypatch) -> None:
    """remove_content 写阶段抛错 → _safe_restore 回写旧全文，md 回到调用前。"""
    import builtins

    from jiuwen_memory.storage.markdown_impl import local_markdown_store as md_module

    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    storage.add(scope, [_doc_unit(scope, "u1", "keep block")])
    storage.add(scope, [_doc_unit(scope, "u2", "remove target")])
    md_path = tmp_path / "memory" / "p1" / "MEMORY.md"
    original_text = md_path.read_text(encoding="utf-8")

    real_open = builtins.open

    def fail_on_write(file, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError("disk write failed")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(md_module, "open", fail_on_write, raising=False)

    with pytest.raises(OSError, match="disk write failed"):
        storage.delete(scope, ["u2"])

    # md 文件回到调用前（remove_content 原子写回滚，未截断）。
    restored = md_path.read_text(encoding="utf-8")
    assert restored == original_text
    assert "keep block" in restored and "remove target" in restored


def test_delete_compensation_restores_removed_md_blocks(tmp_path, monkeypatch) -> None:
    """delete 多块：第 2 块 remove 失败 → shadow 全插回 + 已删第 1 块 md.write 回写。"""
    scope = Scope(org="org", user="user")
    storage = _doc_storage(tmp_path)
    shadow = storage._raw_shadow_index()
    md = storage._raw_markdown()

    storage.add(scope, [_doc_unit(scope, "u1", "block one")])
    storage.add(scope, [_doc_unit(scope, "u2", "block two")])
    md_path = tmp_path / "memory" / "p1" / "MEMORY.md"
    assert "block one" in md_path.read_text(encoding="utf-8")
    assert "block two" in md_path.read_text(encoding="utf-8")

    # 注入 remove_content：第 1 次（u1）正常删，第 2 次（u2）抛错。
    real_remove = md.remove_content
    remove_count = {"n": 0}

    def fail_second_remove(scope_arg, filename, content):
        remove_count["n"] += 1
        if remove_count["n"] == 2:
            raise OSError("remove block two failed")
        return real_remove(scope_arg, filename, content)

    monkeypatch.setattr(md, "remove_content", fail_second_remove)

    # spy 记录 md.restore_blocks 补偿调用（回写已删块）。
    restore_calls: list = []
    real_restore = md.restore_blocks

    def spy_restore(scope_arg, units):
        restore_calls.append([u.id for u in units])
        return real_restore(scope_arg, units)

    monkeypatch.setattr(md, "restore_blocks", spy_restore)

    with pytest.raises(OSError, match="remove block two failed"):
        storage.delete(scope, ["u1", "u2"])

    # shadow 全插回（u1+u2 复活）。
    back = storage.get(scope, ["u1", "u2"])
    assert {u.id for u in back} == {"u1", "u2"}
    # md.restore_blocks 被调回写已删的 u1 块。
    assert restore_calls and "u1" in restore_calls[0]
    # md 文件含回写的 u1 块（block one 回来）。
    restored = md_path.read_text(encoding="utf-8")
    assert "block one" in restored  # 第 1 块被补偿回写
