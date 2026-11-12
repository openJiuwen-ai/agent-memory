# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 实现层回归护栏：装配期绑定、能力声明、错误语义与真实包装层签名。

这些用例都对应「单测用替身、真实实现从未被装配器调用」暴露过的问题，因此断言的是
**真实包装层 / 真实后端类**的行为，不再另造同形替身。
"""

from __future__ import annotations

import inspect
import warnings

import pytest

from jiuwen_memory.common.errors import (
    BackendError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
)
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.storage.domain_store_impl.vector_recaller import VectorRecaller
from jiuwen_memory.storage.entity_impl.in_memory_entity_store import InMemoryEntityStore
from jiuwen_memory.storage.entity_impl.legacy_entity_store_adapter import (
    LegacyEntityStoreAdapter,
)
from jiuwen_memory.storage.entity_impl.shard_routed_entity import ShardRoutedEntityStore
from jiuwen_memory.storage.fulltext_impl.shard_routed_fulltext import (
    ShardRoutedFulltextStore,
)
from jiuwen_memory.storage.kv import ConditionalKV
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.postgres_kv import PostgresKVStore
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore
from jiuwen_memory.storage.kv_impl.shard_placement import placement_key
from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver
from jiuwen_memory.storage.shard_search import STORAGE_SHARD_FIELD, LazyShardLocator
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManagerProducer
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.store_manager_impl.composite_store_manager import (
    _register_shard_topology,
)
from jiuwen_memory.storage.vector_impl.shard_routed_vector import ShardRoutedVectorStore
from tests.support.whitebox import private

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
SHARDS = ("working", "profile")


def _resolver() -> RuleBasedShardResolver:
    return RuleBasedShardResolver(
        shards=list(SHARDS), default_shard="working", rules=[]
    )


def _unit(uid: str):
    from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Segment

    return MemoryUnit(
        id=uid, scope=SCOPE, tier=MemoryTier.CORE, segments=[Segment(content=uid)]
    )


# ---------------------------------------------------------------------------
# 1. ConditionalKV：实现方法必须是实例方法
# ---------------------------------------------------------------------------


class _FakeCrypto:
    @staticmethod
    def encrypt(plaintext: bytes, **kwargs) -> bytes:
        return b"enc:" + plaintext

    @staticmethod
    def decrypt(ciphertext: bytes, **kwargs) -> bytes:
        return ciphertext[4:]

    @staticmethod
    def health() -> None:
        return None


@pytest.mark.parametrize(
    "store_cls",
    [PostgresKVStore, RedisKVStore, InMemoryKVStore],
    ids=["postgres", "redis", "memory"],
)
def test_conditional_kv_methods_are_instance_methods(store_cls) -> None:
    """P0 回归：``create_if_absent`` 一度被误加 ``@staticmethod``。

    静态方法不会绑定 ``self``，真实 PG 部署在第一条 insert 就抛 TypeError；内存实现的
    单测发现不了。这里用 ``getattr_static`` 检查类字典本身（绕过描述符绑定）。
    """
    for name in ConditionalKV.__abstractmethods__:
        member = inspect.getattr_static(store_cls, name)
        assert not isinstance(member, staticmethod), (
            f"{store_cls.__name__}.{name} must be an instance method: "
            "a stray @staticmethod drops the self binding"
        )


def test_encrypted_store_capabilities_do_not_advertise_shard_routed() -> None:
    """加密包装层不能声称自己是分片包装层。"""

    plain = EncryptedKVStore(InMemoryKVStore(), _FakeCrypto())
    assert StoreCapability.SHARD_ROUTED not in plain.capabilities()


def test_encrypted_store_rejects_shard_routed_port() -> None:
    """加密只包物理表：包住分片包装层会双重加密并丢掉逐分片密钥（S09 #1/#9）。"""
    routed = ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={shard: InMemoryKVStore() for shard in SHARDS},
    )
    with pytest.raises(ValidationError):
        EncryptedKVStore(routed, _FakeCrypto())


# ---------------------------------------------------------------------------
# 2. 定位器：装配期绑定，不允许运行期解析
# ---------------------------------------------------------------------------


def test_lazy_locator_fails_fast_before_binding() -> None:
    locator = LazyShardLocator("default")
    assert locator.bound() is False
    with pytest.raises(UnsupportedStorageCapabilityError):
        locator.locate(SCOPE, ["u1"])


class _FixedLocator:
    def __init__(self, shard: str = "profile") -> None:
        self.shard = shard

    def locate(self, scope: Scope, unit_ids: list[str]):
        from jiuwen_memory.storage.shard_routing import ShardLocation

        return ShardLocation(
            found={unit_id: self.shard for unit_id in unit_ids},
            unlocated=frozenset(),
        )


def test_lazy_locator_delegates_after_binding() -> None:
    locator = LazyShardLocator("default")
    locator.bind(_FixedLocator())
    assert locator.locate(SCOPE, ["u1"]).found == {"u1": "profile"}


def test_rebinding_to_another_port_is_rejected() -> None:
    locator = LazyShardLocator("default")
    port = _FixedLocator("working")
    locator.bind(port)
    locator.bind(port)  # 同一对象重复绑定是幂等的
    with pytest.raises(UnsupportedStorageCapabilityError):
        locator.bind(_FixedLocator("working"))


# ---------------------------------------------------------------------------
# 3. 真实包装层的物理标识：装配器传 kind，包装层必须接受
# ---------------------------------------------------------------------------


class _DescribedVector:
    def __init__(self, descriptor: str = "pg:5432/db/vectors") -> None:
        self._descriptor = descriptor

    @staticmethod
    def capabilities():
        return frozenset({StoreCapability.SHARD_PREFILTER})

    @staticmethod
    def score_higher_is_better() -> bool:
        return True

    def resource_descriptor(self) -> str:
        return self._descriptor

    @staticmethod
    def health() -> None:
        return None


class _DescribedFulltext:
    def __init__(self, descriptor: str = "es:9200/fulltext") -> None:
        self._descriptor = descriptor

    @staticmethod
    def capabilities():
        return frozenset({StoreCapability.SHARD_PREFILTER})

    def resource_descriptor(self) -> str:
        return self._descriptor

    @staticmethod
    def health() -> None:
        return None


class _DescribedEntity(InMemoryEntityStore):
    def __init__(self, descriptor: str = "es:9200/entities") -> None:
        super().__init__()
        self._descriptor = descriptor

    def resource_descriptor(self) -> str:
        return self._descriptor


def _real_wrappers() -> dict[str, object]:
    locator = LazyShardLocator("default")
    return {
        "vector:default": ShardRoutedVectorStore(
            resolver=_resolver(),
            locator=locator,
            mode="split",
            stores={
                shard: _DescribedVector(f"pg:5432/db/vectors/{shard}")
                for shard in SHARDS
            },
        ),
        "fulltext:default": ShardRoutedFulltextStore(
            resolver=_resolver(),
            locator=locator,
            mode="split",
            stores={
                shard: _DescribedFulltext(f"es:9200/fulltext/{shard}")
                for shard in SHARDS
            },
        ),
        "entity:default": ShardRoutedEntityStore(
            resolver=_resolver(),
            locator=locator,
            mode="split",
            stores={
                shard: _DescribedEntity(f"es:9200/entities/{shard}")
                for shard in SHARDS
            },
        ),
    }


def test_real_wrappers_accept_assembly_descriptor_kind() -> None:
    """P0 回归：``shard_descriptors`` 曾不接受装配器传入的 ``kind``。

    真实的 ``ShardRoutedVectorStore`` / ``ShardRoutedFulltextStore`` 只接受 ``self``，
    而装配器按 ``provider(label)`` 调用 —— 替身写成 ``kind="..."`` 掩盖了这一点。
    """
    for label, port in _real_wrappers().items():
        descriptors = port.shard_descriptors(label)  # type: ignore[attr-defined]
        assert set(descriptors) == set(SHARDS)
        for mapping in descriptors.values():
            assert list(mapping) == [label]


def test_topology_registration_with_real_wrappers() -> None:
    routed = ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={shard: _DescribedKV(f"pg:5432/{shard}") for shard in SHARDS},
    )
    manager = CompositeStoreManager(kv=routed)
    for label, port in _real_wrappers().items():
        capability, name = label.split(":", 1)
        private(manager, "_named_stores")[StorageCapability(capability)][name] = port
    _register_shard_topology(manager)
    ports = routed.read_topology()["working"].ports
    assert ports["kv:default"] == "pg:5432/working"
    assert ports["vector:default"] == "pg:5432/db/vectors/working"
    assert ports["fulltext:default"] == "es:9200/fulltext/working"
    assert ports["entity:default"] == "es:9200/entities/working"


class _DescribedKV(InMemoryKVStore):
    def __init__(self, descriptor: str) -> None:
        super().__init__()
        self._descriptor = descriptor

    def resource_descriptor(self) -> str:
        return self._descriptor


# ---------------------------------------------------------------------------
# 4. 拓扑更新只覆盖给出的端口
# ---------------------------------------------------------------------------


def test_update_topology_merges_ports_and_expands_short_keys() -> None:
    routed = ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={shard: _DescribedKV(f"pg:5432/{shard}") for shard in SHARDS},
    )
    manager = CompositeStoreManager(kv=routed)
    private(manager, "_named_stores")[StorageCapability.VECTOR]["default"] = _real_wrappers()[
        "vector:default"
    ]
    _register_shard_topology(manager)

    routed.update_topology("working", {"kv": "pg:5432/newdb"})

    ports = routed.read_topology()["working"].ports
    assert ports["kv:default"] == "pg:5432/newdb"
    # 未给出的端口必须保留，否则下次启动的物理身份比对会把迁移判成非法变更。
    assert ports["vector:default"] == "pg:5432/db/vectors/working"


def test_update_topology_rejects_unknown_or_ambiguous_port_keys() -> None:
    routed = ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={shard: _DescribedKV(f"pg:5432/{shard}") for shard in SHARDS},
    )
    _register_shard_topology(CompositeStoreManager(kv=routed))
    with pytest.raises(ValidationError):
        routed.update_topology("working", {"graph": "neo4j://x"})
    with pytest.raises(ValidationError):
        routed.update_topology("working", {"": "x"})


# ---------------------------------------------------------------------------
# 5. 插入协议：非冲突失败也要释放 pending
# ---------------------------------------------------------------------------


class _BrokenKV(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.broken = True

    def insert(self, scope, key, value, ttl: float = 0.0) -> None:
        if self.broken:
            raise BackendError("shard table unavailable")
        super().insert(scope, key, value, ttl)


def test_insert_releases_pending_on_backend_error() -> None:
    placement = InMemoryKVStore()
    broken = _BrokenKV()
    store = ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores={"working": broken, "profile": InMemoryKVStore()},
    )
    with pytest.raises(BackendError):
        store.insert(SCOPE, memory_key("u1"), dumps(_unit("u1")), 0.0)
    # 归属已被释放：该 id 未被 pending 占住，后端恢复后可立即重试。
    assert placement.read_many(SCOPE, [placement_key("u1")]) == {}
    broken.broken = False
    store.insert(SCOPE, memory_key("u1"), dumps(_unit("u1")), 0.0)
    assert store.locate(SCOPE, ["u1"]).found == {"u1": "working"}


# ---------------------------------------------------------------------------
# 6. 装配：locator 由 manager 在端口建好后绑定
# ---------------------------------------------------------------------------


def _build(raw: dict):
    register_backends()
    ctx = AssemblyContext.from_dict(raw)
    return StoreManagerProducer.build_named("main", ctx)


def _sharded_config() -> dict:
    params = {
        "resolver": "default",
        "locator": "default",
        "table_template": {"target": "memory", "params": {"table": "t_{shard}"}},
    }
    return {
        "globals": {
            "store_manager": "main",
            "vector_enabled": False,
            "graph_enabled": False,
        },
        "shard_resolver": {
            "default": {
                "target": "rule_based",
                "params": {
                    "shards": list(SHARDS),
                    "default_shard": "working",
                    "rules": [],
                },
            }
        },
        "kv_store": {
            "default": {
                "target": "shard_routed",
                "params": {
                    "resolver": "default",
                    "passthrough": "legacy",
                    "placement_store": {"target": "memory"},
                    "table_template": {
                        "target": "memory",
                        "params": {"table": "kv_{shard}"},
                    },
                },
            },
            "legacy": {"target": "memory"},
        },
        "vector_store": {
            "default": {"target": "shard_routed", "params": dict(params)},
            "layers_l0": {"target": "shard_routed", "params": dict(params)},
            "layers_l1": {"target": "shard_routed", "params": dict(params)},
        },
        "store_manager": {"main": {"target": "composite"}},
    }


def test_manager_binds_locator_to_assembled_port() -> None:
    manager = _build(_sharded_config())
    vector = private(manager, "_named_stores")[StorageCapability.VECTOR]["default"]
    locator = private(vector, "_locator")
    assert isinstance(locator, LazyShardLocator)
    assert locator.bound() is True
    location = locator.locate(SCOPE, ["u1"])
    # 未写入的记忆：定位成功返回「未定位」而不是抛错（部分结果语义）。
    assert location.found == {}
    assert location.unlocated == frozenset({"u1"})


def test_manager_rejects_locator_port_without_capability() -> None:
    """定位端口必须声明 ``SHARD_LOCATOR``，否则启动失败（不拖到首次 locate）。"""
    raw = _sharded_config()
    for name in ("default", "layers_l0", "layers_l1"):
        raw["vector_store"][name]["params"]["locator"] = "legacy"
    with pytest.raises(ValidationError):
        _build(raw)


# ---------------------------------------------------------------------------
# 7. 能力声明与「不支持」错误语义
# ---------------------------------------------------------------------------


def test_entity_capabilities_follow_mode() -> None:
    locator = LazyShardLocator("default")
    shared = ShardRoutedEntityStore(
        resolver=_resolver(), locator=locator, mode="shared", inner=InMemoryEntityStore()
    )
    split = ShardRoutedEntityStore(
        resolver=_resolver(),
        locator=locator,
        mode="split",
        stores={shard: InMemoryEntityStore() for shard in SHARDS},
    )
    assert StoreCapability.SHARD_PREFILTER in shared.capabilities()
    assert StoreCapability.SHARD_PREFILTER not in split.capabilities()
    for caps in (shared.capabilities(), split.capabilities()):
        assert StoreCapability.SHARD_ROUTED in caps
        assert StoreCapability.GROUPED_LINKS in caps
        assert StoreCapability.ATOMIC_UNLINK in caps


class _PrefilterOnlyFulltext:
    @staticmethod
    def capabilities():
        return frozenset({StoreCapability.SHARD_PREFILTER})

    @staticmethod
    def resource_descriptor() -> None:
        return None


class _PrefilterOnlyVector(_PrefilterOnlyFulltext):
    @staticmethod
    def score_higher_is_better() -> bool:
        return True


class _PrefilterOnlyEntity(InMemoryEntityStore):
    @staticmethod
    def capabilities():
        return frozenset({StoreCapability.SHARD_PREFILTER})

    @staticmethod
    def resource_descriptor() -> None:
        return None


class _PortManager:
    """只暴露 recaller 需要的最小端口集。"""

    def __init__(self, *, fulltext=None, vector=None, entity=None) -> None:
        self._fulltext = fulltext
        self._vector = vector
        self._entity = entity

    def has_fulltext(self, name: str = "default") -> bool:
        return self._fulltext is not None

    def fulltext(self, name: str = "default"):
        return self._fulltext

    def has_vector(self, name: str = "default") -> bool:
        return self._vector is not None

    def vector(self, name: str = "default"):
        return self._vector

    @staticmethod
    def has_kv(name: str = "default") -> bool:
        return False

    @staticmethod
    def kv(name: str = "default"):
        raise UnsupportedStorageCapabilityError("no kv port")


def test_keyword_recaller_requires_routed_fulltext_not_prefilter() -> None:
    """``SHARD_PREFILTER`` 只说明「给条件就能前置过滤」，不等于本通道会注入条件。"""
    recaller = KeywordRecaller(
        _PortManager(fulltext=_PrefilterOnlyFulltext()),
        entity_store=_PrefilterOnlyEntity(),
    )
    assert recaller.supports_shards() is False

    routed = ShardRoutedFulltextStore(
        resolver=_resolver(),
        locator=LazyShardLocator("default"),
        mode="split",
        stores={shard: _PrefilterOnlyFulltext() for shard in SHARDS},
    )
    assert KeywordRecaller(_PortManager(fulltext=routed)).supports_shards() is True


def test_vector_recaller_requires_routed_port() -> None:
    assert VectorRecaller(_PortManager(vector=_PrefilterOnlyVector())).supports_shards() is False

    routed = ShardRoutedVectorStore(
        resolver=_resolver(),
        locator=LazyShardLocator("default"),
        mode="split",
        stores={shard: _PrefilterOnlyVector() for shard in SHARDS},
    )
    assert VectorRecaller(_PortManager(vector=routed)).supports_shards() is True


# ---------------------------------------------------------------------------
# 8. 旧实体适配器：无法履约的操作必须报失败
# ---------------------------------------------------------------------------


class _LegacyStore:
    @staticmethod
    def store_type():
        return InMemoryEntityStore().store_type()

    @staticmethod
    def health() -> None:
        return None

    @staticmethod
    def ensure_index() -> None:
        return None

    @staticmethod
    def find_by_entity_text_hash(space_id, hashes, *, filters, limit=500):
        return []

    @staticmethod
    def find_by_linked_memory_id(space_id, memory_id, *, filters):
        return []

    @staticmethod
    def execute_operations(space_id, operations):
        return EntityBatchResult(
            successful_ids=[op.record_id or "" for op in operations], failed_ids=[]
        )

    @staticmethod
    def search(space_id, query_vector, *, top_k, filters):
        return []


def test_legacy_adapter_reports_delete_if_empty_as_failed() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        adapter = LegacyEntityStoreAdapter(_LegacyStore())
    result = adapter.execute_operations(
        SCOPE,
        [
            EntityOperation(
                type=EntityOpType.UNLINK, record_id="e1", unlink_memory_ids=("u1",)
            ),
            EntityOperation(type=EntityOpType.DELETE_IF_EMPTY, record_id="e2"),
        ],
    )
    assert "e2" in result.failed_ids
    assert "e2" not in result.successful_ids


# ---------------------------------------------------------------------------
# 9. Redis ConditionalKV：集群安全的部分读 + 替换保留 TTL
# ---------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, values: list[bytes | None]) -> None:
        self._values = values
        self.requested: list[str] = []

    def get(self, name: str) -> None:
        self.requested.append(name)

    def execute(self):
        return self._values


class _FakeRedisClient:
    """只实现本用例用到的命令；``MGET`` 直接失败，防止退回跨 slot 写法。"""

    def __init__(self, values: list[bytes | None]) -> None:
        self._values = values
        self.pipelines: list[_FakePipeline] = []

    def pipeline(self, transaction: bool = False):
        pipe = _FakePipeline(self._values)
        self.pipelines.append(pipe)
        return pipe

    @staticmethod
    def mget(names):  # pragma: no cover - 出现即失败
        raise AssertionError("read_many must not use MGET (Redis Cluster CROSSSLOT)")


class _FakeClientRedis(RedisKVStore):
    def __init__(self, fake: _FakeRedisClient) -> None:
        super().__init__(url="redis://example.invalid:6379/0")
        self._fake = fake

    @property
    def client(self):
        return self._fake


def test_redis_read_many_uses_per_key_get() -> None:
    fake = _FakeRedisClient([b"a", None, b"c"])
    store = _FakeClientRedis(fake)
    result = store.read_many(SCOPE, ["k1", "k2", "k3"])
    assert result == {"k1": b"a", "k3": b"c"}
    assert len(fake.pipelines) == 1
    assert [name.rsplit(":", 1)[-1] for name in fake.pipelines[0].requested] == [
        "k1",
        "k2",
        "k3",
    ]


def test_redis_conditional_replace_preserves_ttl() -> None:
    """比较后替换必须保留原 TTL（``PTTL`` + ``PX``，不抬高 Redis 版本门槛）。"""
    script = private(RedisKVStore, "_CAS_SCRIPT")
    assert "PTTL" in script and "PX" in script
    assert "KEEPTTL" not in script


# ---------------------------------------------------------------------------
# 10. 归属存储缺 ConditionalKV 时的诊断
# ---------------------------------------------------------------------------


def test_placement_store_without_conditional_kv_gets_actionable_error(tmp_path) -> None:
    from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

    with pytest.raises(UnsupportedStorageCapabilityError) as excinfo:
        ShardRoutedKVStore(
            resolver=_resolver(),
            passthrough=InMemoryKVStore(),
            placement=SQLiteKVStore(db_path=str(tmp_path / "placement.db")),
            shard_stores={shard: InMemoryKVStore() for shard in SHARDS},
        )
    message = str(excinfo.value)
    assert "conditional_kv" in message
    assert "SQLite" in message


# ---------------------------------------------------------------------------
# 11. 连接预算：按对象身份去重后汇总（含包装层内联依赖）
# ---------------------------------------------------------------------------


class _BudgetConfig:
    def __init__(self, budget) -> None:
        self._budget = budget

    def get(self, key, default=None):
        if key == "storage_connection_budget":
            return self._budget
        return default


class _PooledStore:
    def __init__(self, size: int, label: str) -> None:
        self._size = size
        self._label = label

    def connection_budget(self) -> int:
        return self._size

    @staticmethod
    def connection_dependencies():
        return ()

    def resource_descriptor(self) -> str:
        return self._label


class _WrapperWithDeps:
    """包装层替身：自己不计预算，只暴露物理子依赖（与 BaseStore 默认一致）。"""

    def __init__(self, children: list[_PooledStore]) -> None:
        self._children = children

    @staticmethod
    def connection_budget() -> int | None:
        return None

    def connection_dependencies(self):
        return tuple(self._children)


class _PortManagerStub:
    def __init__(self, ports) -> None:
        self._ports = ports

    def iter_named_ports(self):
        return self._ports


def test_connection_budget_sums_inline_dependencies_once() -> None:
    from jiuwen_memory.storage.store_manager_impl.composite_store_manager import (
        _validate_connection_budget,
    )

    children = [_PooledStore(8, f"pg:5432/db/t{i}") for i in range(10)]
    wrapper = _WrapperWithDeps(children)
    ports = [
        ("kv", "default", wrapper),
        # 同一个物理实例又以端口形式出现：按对象身份去重，只能算一次。
        ("kv", "inline", children[0]),
    ]
    manager = _PortManagerStub(ports)
    _validate_connection_budget(_BudgetConfig(None), manager)  # 未配预算只汇总
    _validate_connection_budget(_BudgetConfig(100), manager)  # 80 <= 100
    with pytest.raises(ValidationError) as excinfo:
        _validate_connection_budget(_BudgetConfig(50), manager)
    assert "80 > 50" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 12. 分片标签不再依赖 common 的内置字段 / 端口集合按能力读取
# ---------------------------------------------------------------------------


def test_shard_tag_needs_no_common_builtin() -> None:
    from jiuwen_memory.common.type_def.filter import canonical_filter_field

    assert STORAGE_SHARD_FIELD.startswith("system_metadata.")
    assert canonical_filter_field(STORAGE_SHARD_FIELD) == STORAGE_SHARD_FIELD
    # 裸 ``storage_shard`` 只是普通用户 metadata 字段，common 层不再为 storage 特判。
    assert canonical_filter_field("storage_shard") == "user_metadata.storage_shard"


def test_manager_exposes_ports_and_configured_shards() -> None:
    from jiuwen_memory.storage.shard_search import configured_shards
    routed = ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={shard: InMemoryKVStore() for shard in SHARDS},
    )
    manager = CompositeStoreManager(kv=routed)
    assert (StorageCapability.KV, "default") in {
        (capability, name) for capability, name, _ in manager.iter_named_ports()
    }
    assert configured_shards(routed) == SHARDS
    with pytest.raises(UnsupportedStorageCapabilityError):
        configured_shards(InMemoryKVStore())


# ---------------------------------------------------------------------------
# 13. 旧实体适配器按契约版本识别，不猜方法签名
# ---------------------------------------------------------------------------


def test_legacy_adapter_detects_contract_version() -> None:
    from jiuwen_memory.storage.entity_impl.legacy_entity_store_adapter import (
        _contract_version,
    )
    from jiuwen_memory.storage.entity_store import EntityStore

    class _NewContract(InMemoryEntityStore):
        pass

    assert _contract_version(_NewContract()) == EntityStore.CONTRACT_VERSION
    assert _contract_version(object()) == 1


# ---------------------------------------------------------------------------
# 14. 装配协议：分片包装层按能力承诺实现，装配器不再逐方法探测
# ---------------------------------------------------------------------------


def test_shard_routed_ports_implement_assembly_protocol() -> None:
    from jiuwen_memory.storage.shard_port import (
        ShardLocatorConsumer,
        ShardPlacementOwner,
        ShardRoutedPort,
    )

    locator = LazyShardLocator("default")
    routed = ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={shard: InMemoryKVStore() for shard in SHARDS},
    )
    wrappers = [
        ShardRoutedVectorStore(
            resolver=_resolver(),
            locator=locator,
            mode="split",
            stores={
                shard: _DescribedVector(f"pg:5432/db/vectors/{shard}")
                for shard in SHARDS
            },
        ),
        ShardRoutedFulltextStore(
            resolver=_resolver(),
            locator=locator,
            mode="split",
            stores={
                shard: _DescribedFulltext(f"es:9200/fulltext/{shard}")
                for shard in SHARDS
            },
        ),
        ShardRoutedEntityStore(
            resolver=_resolver(),
            locator=locator,
            mode="split",
            stores={shard: InMemoryEntityStore() for shard in SHARDS},
        ),
    ]
    for port in [routed, *wrappers]:
        assert isinstance(port, ShardRoutedPort)
        assert StoreCapability.SHARD_ROUTED in port.capabilities()
    assert isinstance(routed, ShardPlacementOwner)
    assert StoreCapability.SHARD_PLACEMENT in routed.capabilities()
    for port in wrappers:
        assert isinstance(port, ShardLocatorConsumer)


# ---------------------------------------------------------------------------
# 15. 搬迁装配校验（F09 D16）
# ---------------------------------------------------------------------------


def test_relocation_requires_relocatable_derived_ports() -> None:
    from jiuwen_memory.storage.store_manager_impl.composite_store_manager import (
        _bind_shard_relocation,
    )

    class _Owner:
        def __init__(self) -> None:
            self.bound = ()

        @staticmethod
        def capabilities():
            return frozenset(
                {
                    StoreCapability.SHARD_ROUTED,
                    StoreCapability.SHARD_LOCATOR,
                    StoreCapability.SHARD_PLACEMENT,
                }
            )

        @staticmethod
        def relocation_enabled() -> bool:
            return True

        def bind_relocatable_ports(self, ports) -> None:
            self.bound = tuple(ports)

    class _PlainDerived:
        @staticmethod
        def capabilities():
            return frozenset({StoreCapability.SHARD_ROUTED})

        @staticmethod
        def locator_name() -> str:
            return "default"  # 跟随 kv:default，因此受其搬迁开关约束

    class _Manager:
        @staticmethod
        def iter_named_ports():
            return [
                (StorageCapability.KV, "default", _Owner()),
                (StorageCapability.VECTOR, "default", _PlainDerived()),
            ]

    with pytest.raises(ValidationError, match="SHARD_RELOCATABLE"):
        _bind_shard_relocation(_Manager())


def _milvus_collection(collection: str):
    from jiuwen_memory.storage.vector_impl.milvus_vector import MilvusVectorStore

    return MilvusVectorStore(
        uri="http://milvus:19530", collection=collection, dim=4
    )


def test_vector_split_milvus_collection_is_the_physical_identity() -> None:
    # 同一实例、不同 collection 是最常见的 split 部署，必须允许。
    store = ShardRoutedVectorStore(
        resolver=_resolver(),
        locator=LazyShardLocator("default"),
        mode="split",
        stores={
            "working": _milvus_collection("memory_working"),
            "profile": _milvus_collection("memory_profile"),
        },
    )
    assert store.shards == ("working", "profile")

    # 同一实例、同一 collection 才是同一物理资源。
    with pytest.raises(ValidationError, match="same physical resource"):
        ShardRoutedVectorStore(
            resolver=_resolver(),
            locator=LazyShardLocator("default"),
            mode="split",
            stores={
                "working": _milvus_collection("memory_shared"),
                "profile": _milvus_collection("memory_shared"),
            },
        )
