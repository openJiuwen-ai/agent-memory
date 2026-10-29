"""垂直切片：两级命名空间下 index_builder 与 recaller 的 VectorStore 共享语义。

F07 语义：共享的载体是**全局唯一 StoreManager 的命名端口**——具名 VectorStore
（如 ``vector_store.shared_vec``）经全量自动成为 manager 端口，消费者用
``params.vector_store: <端口名>`` 引用同一端口 → 同一实例。

- 显式具名 + 同名引用 → 两者拿到**同一个** VectorStore（写读一致的来源）。
- 零配置（各自默认端口）→ 仍共享 manager 的 default 端口实例；要独立须指名
  不同端口。已确认的取舍：要跨消费者共享/隔离须显式配置端口名。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import DedupDecision, LifecycleState
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.evolver import EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import DynamicEvolver
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.domain_store_impl.recaller import RecallerProducer
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from tests.unit.construction.fixtures import named_truth_evolver_context
from tests.unit.construction.test_temporal_validity import _fact

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _bootstrap():
    register_plugins()
    register_backends()
    register_operators()
    register_constructors()
    Factory.reset_all()
    yield
    Factory.reset_all()


def test_reference_shares_vector_port():
    """ib 与 rec 指名同一 vector 端口 → 同一实例（写读一致来源）。"""
    ctx = AssemblyContext.from_dict(
        {
            "kv_store": {"default": {"target": "memory"}},
            "vector_store": {
                "shared_vec": {"target": "memory"},
            },
            "store_manager": {"default": {"target": "composite"}},
            "constructor": {
                "ib": {
                    "target": "vector",
                    "params": {"vector_store": "shared_vec"},
                }
            },
            "recaller": {
                "rec": {
                    "target": "vector",
                    "params": {"vector_store": "shared_vec"},
                }
            },
        }
    )
    manager = StoreManagerProducer.build_named("default", ctx)
    ib = IndexBuilderProducer.build_named("ib", ctx)
    rec = RecallerProducer.build_named("rec", ctx)
    # 同名端口 → 共享（与 manager.vector("shared_vec") 同一实例）
    assert ib._vector_store is manager.vector("shared_vec")  # pylint: disable=protected-access
    assert rec.vector_store is manager.vector("shared_vec")


def test_zero_config_uses_default_port_shared_by_manager():
    """零配置 → 两者都落 manager 的 default 端口（同一实例，F07 全局共享语义）。"""
    ctx = AssemblyContext.from_dict(
        {
            "kv_store": {"default": {"target": "memory"}},
            "vector_store": {"default": {"target": "memory"}},
            "store_manager": {"default": {"target": "composite"}},
            "constructor": {"ib": "vector"},
            "recaller": {"rec": "vector"},
        }
    )
    manager = StoreManagerProducer.build_named("default", ctx)
    ib = IndexBuilderProducer.build_named("ib", ctx)
    rec = RecallerProducer.build_named("rec", ctx)
    assert ib._vector_store is manager.vector()  # pylint: disable=protected-access
    assert rec.vector_store is manager.vector()


@pytest.mark.parametrize("kind", ["orchestrating", "dynamic"])
@pytest.mark.parametrize("schema_enabled", [False, True])
@pytest.mark.parametrize("default_has_record", [False, True])
@pytest.mark.parametrize("decision", [DedupDecision.UPDATE, DedupDecision.SUPERSEDE])
@pytest.mark.parametrize("builder", ["unified", "forward", "hybrid"])
def test_evolver_rechecks_the_writers_named_truth(
    kind, schema_enabled, default_has_record, decision, builder,
):
    # pylint: disable=protected-access  # 标准 Producer 装配后验证修改型决策的真源一致性
    ctx = named_truth_evolver_context(kind, schema_enabled=schema_enabled, builder=builder)
    manager = StoreManagerProducer.build_named("default", ctx)
    index = IndexBuilderProducer.build_named("writer", ctx)
    evolver = EvolverProducer.build_named("worker", ctx)
    truth = manager.domain_store("truth")
    initial, candidate = _fact("a", 1), _fact("b", 2)
    index.build([initial])
    if default_has_record:
        manager.domain_store().add(initial.scope, [_fact("a", 1)])
        original_default = manager.domain_store().get(initial.scope, [initial.id])
        original_default[0].segments[0].content = "wrong body from default domain"
        manager.domain_store().update(initial.scope, original_default)
    original_default = manager.domain_store().get(initial.scope, [initial.id])

    result = EvolveResult()
    evolver._apply_decision(candidate, decision, initial, 0.8, result)

    stored = truth.get(initial.scope, [initial.id])[0]
    assert manager.domain_store().get(initial.scope, [initial.id]) == original_default
    if decision == DedupDecision.UPDATE:
        assert "wrong body" not in stored.content
        assert result.updated_ids == [initial.id]
        assert stored.temporal.t_message == candidate.temporal.t_message
        assert truth.get(candidate.scope, [candidate.id]) == []
    else:
        assert stored.content == initial.content, "归档不能把默认库正文复制到具名真源"
        assert stored.lifecycle == LifecycleState.SUPERSEDED
        assert result.superseded_ids == [initial.id]
        candidate = truth.get(candidate.scope, [candidate.id])[0]
        assert candidate.lifecycle == LifecycleState.ACTIVE and candidate.supersedes == initial.id
        assert candidate.system_metadata["dedup_temporal_reason"] == "newer_statement"


@pytest.mark.parametrize("kind", ["orchestrating", "dynamic"])
@pytest.mark.parametrize("schema_enabled", [False, True])
@pytest.mark.parametrize("builder", ["unified", "forward", "hybrid"])
def test_named_truth_retry_finishes_partial_supersede(kind, schema_enabled, builder, monkeypatch):
    # pylint: disable=protected-access  # 故障注入验证具名真源中的候选复用与旧版补归档
    ctx = named_truth_evolver_context(kind, schema_enabled=schema_enabled, builder=builder)
    manager = StoreManagerProducer.build_named("default", ctx)
    index = IndexBuilderProducer.build_named("writer", ctx)
    evolver = EvolverProducer.build_named("worker", ctx)
    truth = manager.domain_store("truth")
    initial, candidate = _fact("a", 1), _fact("b", 2)
    index.build([initial])
    original_update = index.update

    def fail_retirement(units, **kwargs):
        if units[0].id == initial.id:
            raise RuntimeError("retirement failed")
        return original_update(units, **kwargs)

    monkeypatch.setattr(index, "update", fail_retirement)
    with pytest.raises(RuntimeError, match="retirement failed"):
        evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, initial, 0.8, EvolveResult())
    assert truth.get(candidate.scope, [candidate.id])[0].supersedes == initial.id
    assert truth.get(initial.scope, [initial.id])[0].lifecycle == LifecycleState.ACTIVE

    def unexpected_build(_units, **_kwargs):
        pytest.fail("重试必须复用具名真源中的候选，不能重新 build")

    monkeypatch.setattr(index, "update", original_update)
    monkeypatch.setattr(index, "build", unexpected_build)
    result = EvolveResult()
    evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, initial, 0.8, result)
    assert result.superseded_ids == [initial.id] and result.created_ids == [candidate.id]
    assert truth.get(initial.scope, [initial.id])[0].lifecycle == LifecycleState.SUPERSEDED
    assert manager.domain_store().get(initial.scope, [initial.id, candidate.id]) == []


@pytest.mark.parametrize("kind", [OrchestratingEvolver, DynamicEvolver])
def test_manual_evolver_accepts_exact_writer_reader(kind):
    # pylint: disable=protected-access  # 直接构造兼容：不从底层 KV 或 builder 私有字段推断真源
    ctx = named_truth_evolver_context("orchestrating")
    manager = StoreManagerProducer.build_named("default", ctx)
    truth = manager.domain_store("truth")
    index = IndexBuilderProducer.build("forward", {"kv_store": "truth"}, ctx)
    assert index.read_domain_store().get(_fact("a", 1).scope, ["a"]) == []
    evolver = kind(
        extractor=object(), abstractor=object(), associator=object(),
        index_builder=index, storage=manager, message_store=manager.kv(),
        dedup=object(), llm=object(), domain_store=index.read_domain_store(),
    )
    initial, candidate = _fact("a", 1), _fact("b", 2)
    index.build([initial])
    result = EvolveResult()

    evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, initial, 0.8, result)

    assert truth.get(initial.scope, [initial.id])[0].lifecycle == LifecycleState.SUPERSEDED
    assert result.superseded_ids == [initial.id]
    assert truth.get(candidate.scope, [candidate.id])[0].supersedes == initial.id
    assert manager.domain_store().get(initial.scope, [initial.id, candidate.id]) == []


@pytest.mark.parametrize("kind", ["orchestrating", "dynamic"])
@pytest.mark.parametrize("schema_enabled", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_builder_requires_explicit_read_domain(kind, schema_enabled, explicit, monkeypatch):
    ctx = named_truth_evolver_context(kind, schema_enabled=schema_enabled)
    manager = StoreManagerProducer.build_named("default", ctx)
    index = IndexBuilderProducer.build_named("writer", ctx)
    monkeypatch.setattr(index, "read_domain_store", lambda: None)
    params = {"index_builder": "writer", "extractor": "keyword", "dedup": "keyword"}
    if not explicit:
        with pytest.raises(ValidationError, match="params.domain_store"):
            EvolverProducer.build(kind, params, ctx)
        return
    params["domain_store"] = "truth"
    evolver = EvolverProducer.build(kind, params, ctx)
    initial, candidate = _fact("a", 1), _fact("b", 2)
    index.build([initial])
    result = EvolveResult()
    # pylint: disable=protected-access  # 模拟旧 builder 不声明可选能力，由配置显式绑定
    evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, initial, 0.8, result)
    assert result.superseded_ids == [initial.id]
    stored = manager.domain_store("truth").get(initial.scope, [initial.id])[0]
    assert stored.content == initial.content
    assert manager.domain_store().get(initial.scope, [initial.id, candidate.id]) == []


@pytest.mark.parametrize("kind", ["orchestrating", "dynamic"])
def test_conflicting_explicit_read_domain_is_rejected(kind):
    ctx = named_truth_evolver_context(kind)
    with pytest.raises(ValidationError, match="conflicts"):
        EvolverProducer.build(kind, {
            "index_builder": "writer", "extractor": "keyword", "dedup": "keyword",
            "domain_store": "default",
        }, ctx)


@pytest.mark.parametrize("kind", [OrchestratingEvolver, DynamicEvolver])
def test_manual_unknown_truth_fails_before_writing(kind, monkeypatch):
    # pylint: disable=protected-access  # 直接构造也不得在缺真源声明时回落默认库
    ctx = named_truth_evolver_context("orchestrating")
    manager = StoreManagerProducer.build_named("default", ctx)
    index = IndexBuilderProducer.build_named("writer", ctx)
    monkeypatch.setattr(index, "read_domain_store", lambda: None)
    evolver = kind(
        extractor=object(), abstractor=object(), associator=object(), index_builder=index,
        storage=manager, message_store=manager.kv(), dedup=object(), llm=object(),
    )
    with pytest.raises(ValidationError, match="write-bound truth reader"):
        evolver._apply_decision(_fact("a", 1), DedupDecision.ADD, None, 0.0, EvolveResult())
    assert manager.domain_store("truth").get(_fact("a", 1).scope, ["a"]) == []


@pytest.mark.parametrize("resource", ["memory_unit", "kv"])
def test_forward_truth_reader_preserves_both_authorization_layers(resource, monkeypatch):
    ctx = named_truth_evolver_context("orchestrating", builder="forward")
    manager = StoreManagerProducer.build_named("default", ctx)
    index = IndexBuilderProducer.build_named("writer", ctx)
    initial = _fact("a", 1)
    index.build([initial])

    def deny_selected_resource(_access, _scope, _action, requested):
        if requested == resource:
            raise PermissionDeniedError(message="truth read denied")

    monkeypatch.setattr(manager.security, "authorize", deny_selected_resource)
    with pytest.raises(PermissionDeniedError, match="truth read denied"):
        index.read_domain_store().get(initial.scope, [initial.id])


@pytest.mark.parametrize("kind", [OrchestratingEvolver, DynamicEvolver])
@pytest.mark.parametrize("builder", ["forward", "hybrid", "unified"])
def test_manual_conflicting_reader_rejected_before_cross_store_write(kind, builder):
    ctx = named_truth_evolver_context("orchestrating", builder=builder)
    manager = StoreManagerProducer.build_named("default", ctx)
    index = IndexBuilderProducer.build_named("writer", ctx)
    original, unrelated = _fact("same-id", 1), _fact("same-id", 2)
    unrelated.segments[0].content = "Different body in the default data plane"
    index.build([original])
    manager.domain_store().add(unrelated.scope, [unrelated])

    with pytest.raises(ValidationError, match="omit domain_store"):
        kind(
            extractor=object(), abstractor=object(), associator=object(),
            index_builder=index, storage=manager, message_store=manager.kv(),
            dedup=object(), llm=object(), domain_store=manager.domain_store(),
        )

    assert manager.domain_store("truth").get(original.scope, [original.id]) == [original]
    assert manager.domain_store().get(unrelated.scope, [unrelated.id]) == [unrelated]


@pytest.mark.parametrize("kind", ["orchestrating", "dynamic"])
@pytest.mark.parametrize("builder", ["forward", "hybrid"])
def test_same_kv_distinct_facade_requires_writer_binding(kind, builder):
    ctx = named_truth_evolver_context(kind, builder=builder)
    params = {"index_builder": "writer", "extractor": "keyword", "dedup": "keyword"}
    with pytest.raises(ValidationError, match="sharing a KV is insufficient"):
        EvolverProducer.build(kind, {**params, "domain_store": "truth"}, ctx)
    evolver = EvolverProducer.build(kind, params, ctx)
    initial, candidate = _fact("a", 1), _fact("b", 2)
    index = IndexBuilderProducer.build_named("writer", ctx)
    index.build([initial])
    # pylint: disable=protected-access
    evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, initial, 0.8, EvolveResult())
    manager = StoreManagerProducer.build_named("default", ctx)
    assert manager.domain_store("truth").get(initial.scope, [initial.id])[0].lifecycle == (
        LifecycleState.SUPERSEDED
    )
