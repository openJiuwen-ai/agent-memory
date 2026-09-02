from __future__ import annotations

import json
import re
from xml.etree import ElementTree

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import (
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.config.config import Config
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.evolver import EvolveMode, EvolverProducer
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import DynamicEvolver
from jiuwen_memory.construction.extractor import Extractor
from jiuwen_memory.construction.extractor_impl.dynamic_llm_extractor import DynamicLLMExtractor
from jiuwen_memory.construction.extractor_impl.llm_extractor import (
    InvalidExtractionCandidateError,
    InvalidExtractionJSONError,
)
from jiuwen_memory.construction.prompt_registry import (
    PHASE_CONSOLIDATE,
    PHASE_EXTRACT,
    PHASE_REFLECT,
    PromptRegistry,
)
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
from jiuwen_memory.storage.types import IndexWriteMode

_TEST_KEY_HEX = "00" * 32


class _ScriptedLLM(LLM):
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.messages: list[list] = []

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages, **options):
        self.messages.append(messages)
        if self.responses:
            return self.responses.pop(0)
        source_id = re.search(r"\[ID: ([^\]]+)\]", messages[-1].content).group(1)
        return json.dumps(
            [
                {
                    "source_id": source_id,
                    "content": "动态抽取结果",
                    "target": "fact",
                    "tier": "semantic",
                    "tags": ["dynamic"],
                    "confidence": 1.0,
                }
            ],
            ensure_ascii=False,
        )


class _FallbackExtractor(Extractor):
    def __init__(self) -> None:
        self.called = False

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(self, units, *, context=None):
        self.called = True
        return list(units)


class _XmlDynamicExtractor(DynamicLLMExtractor):
    def parse_response(self, response, sources, strategy):
        source_map = {unit.id: unit for unit in sources}
        result = []
        for item in ElementTree.fromstring(response).findall("memory"):
            source = source_map.get(item.attrib.get("source_id", ""))
            content = (item.text or "").strip()
            if source is None or not content:
                continue
            result.append(
                MemoryUnit(
                    id=f"{source.id}-{strategy}",
                    scope=source.scope,
                    tier=MemoryTier.SEMANTIC,
                    segments=[Segment(content=content, source=source.source)],
                    provenance=[source.id],
                    system_metadata={"parser_format": "xml"},
                )
            )
        return result


class _FailingStrategyExtractor(DynamicLLMExtractor):
    def parse_response(self, response, sources, strategy):
        if strategy == "broken":
            raise ValueError("broken strategy")
        return super().parse_response(response, sources, strategy)


class _Dedup:
    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])

    def recall(self, candidate):
        return list(self.hits)


class _Index:
    """记录调用并交付 Storage 的替身——IndexBuilder 是记忆写入的唯一入口。"""

    def __init__(self, storage=None) -> None:
        self.built = []
        self.updated = []
        self._storage = storage

    def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        self.built.extend(units)
        if self._storage is not None:
            for unit in units:
                self._storage.add(unit.scope, [unit])

    def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        self.updated.extend(units)
        if self._storage is not None:
            for unit in units:
                self._storage.update(unit.scope, [unit])

    def remove(self, unit_ids):
        pass

    def rebuild(self):
        pass


def _unit(unit_id: str, content: str, metadata=None) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="org", user="user"),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
        system_metadata=dict(metadata or {}),
    )


def _make_evolver(
    *,
    llm: _ScriptedLLM | None = None,
    dedup_hits=None,
    prompts: dict | None = None,
) -> tuple[DynamicEvolver, InMemoryKVStore, _Index]:
    kv = InMemoryKVStore()
    storage = CompositeStorage(kv=kv, graph=InMemoryGraphStore())
    index = _Index(storage)
    extractor = _FallbackExtractor()
    dedup = _Dedup(dedup_hits)
    registry = PromptRegistry.from_dict(prompts or {})
    evolver = DynamicEvolver(
        extractor=extractor,
        abstractor=object(),  # EXTRACT 路径不触发 abstractor
        associator=object(),  # EXTRACT 路径不触发 associator
        index_builder=index,
        storage=storage,
        message_store=storage.kv,
        dedup=dedup,
        llm=llm or _ScriptedLLM(),
        layer_annotator=None,
        prompt_registry=registry,
    )
    return evolver, kv, index


# ----------------------------------------------------------------------
# DynamicLLMExtractor 模板方法测试
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_dynamic_extractor_runs_each_custom_strategy_and_keeps_consolidation_prompt():
    llm = _ScriptedLLM()
    fallback = _FallbackExtractor()
    source = _unit(
        "source-1",
        "原始内容",
        {
            "_extract_prompt_episodic": "episodic_key",
            "_extract_prompt_custom": "custom_key",
            "_consolidation_prompt_episodic": "按事件时序巩固",
        },
    )
    registry = PromptRegistry.from_dict(
        {
            "extract": {
                "episodic_key": "只抽取事件",
                "custom_key": "只抽取自定义事实",
            }
        }
    )
    extractor = DynamicLLMExtractor(llm, fallback, prompt_registry=registry)

    result = extractor.extract([source])

    assert len(result) == 2
    assert [unit.system_metadata["_extraction_strategy"] for unit in result] == [
        "episodic",
        "custom",
    ]
    assert all(
        unit.system_metadata["_consolidation_prompt_episodic"] == "按事件时序巩固"
        for unit in result
    )
    assert llm.messages[0][0].content == "只抽取事件"
    assert fallback.called is False


@pytest.mark.unit
def test_dynamic_extractor_without_prompt_delegates_to_fallback():
    fallback = _FallbackExtractor()
    source = _unit("source-1", "原始内容")

    result = DynamicLLMExtractor(_ScriptedLLM(), fallback).extract([source])

    assert result == [source]
    assert fallback.called is True


@pytest.mark.unit
def test_dynamic_extractor_subclass_can_parse_xml_into_memory_units():
    llm = _ScriptedLLM(
        ['<memories><memory source_id="source-1">XML抽取结果</memory></memories>']
    )
    fallback = _FallbackExtractor()
    source = _unit(
        "source-1",
        "原始内容",
        {
            "_extract_prompt_xml": "xml_key",
            "_consolidation_prompt_xml": "按 XML 策略巩固",
        },
    )
    registry = PromptRegistry.from_dict(
        {
            "extract": {
                "xml_key": (
                    "按 XML 格式抽取："
                    '<memories><memory source_id="...">...</memory></memories>'
                )
            }
        }
    )

    result = _XmlDynamicExtractor(
        llm, fallback, prompt_registry=registry
    ).extract([source])

    assert len(result) == 1
    assert isinstance(result[0], MemoryUnit)
    assert result[0].content == "XML抽取结果"
    assert result[0].provenance == ["source-1"]
    assert result[0].system_metadata["parser_format"] == "xml"
    assert result[0].system_metadata["_extraction_strategy"] == "xml"
    assert result[0].system_metadata["_consolidation_prompt_xml"] == "按 XML 策略巩固"
    assert llm.messages[0][0].content == (
        "按 XML 格式抽取："
        '<memories><memory source_id="...">...</memory></memories>'
    )
    assert fallback.called is False


@pytest.mark.unit
def test_dynamic_extractor_subclass_failure_isolated_per_strategy():
    source = _unit(
        "source-1",
        "原始内容",
        {
            "_extract_prompt_broken": "broken_key",
            "_extract_prompt_json": "json_key",
        },
    )
    registry = PromptRegistry.from_dict(
        {"extract": {"broken_key": "无法解析的策略", "json_key": "正常 JSON 策略"}}
    )

    result = _FailingStrategyExtractor(
        _ScriptedLLM(), _FallbackExtractor(), prompt_registry=registry
    ).extract([source])

    assert len(result) == 1
    assert result[0].system_metadata["_extraction_strategy"] == "json"


@pytest.mark.unit
def test_dynamic_extractor_raises_when_every_strategy_payload_is_invalid():
    source = _unit(
        "source-1",
        "原始内容",
        {"_extract_prompt_broken": "broken_key"},
    )
    registry = PromptRegistry.from_dict(
        {"extract": {"broken_key": "返回 JSON 数组"}}
    )
    extractor = DynamicLLMExtractor(
        _ScriptedLLM(["not json"]),
        _FallbackExtractor(),
        prompt_registry=registry,
    )

    with pytest.raises(InvalidExtractionJSONError):
        extractor.extract([source])


@pytest.mark.unit
def test_dynamic_extractor_raises_for_invalid_candidate_structure():
    source = _unit(
        "source-1",
        "原始内容",
        {"_extract_prompt_broken": "broken_key"},
    )
    response = json.dumps(
        [{"source_id": "missing", "content": "orphan fact", "confidence": 1.0}]
    )
    extractor = DynamicLLMExtractor(
        _ScriptedLLM([response]),
        _FallbackExtractor(),
        prompt_registry=PromptRegistry.from_dict(
            {"extract": {"broken_key": "返回 JSON 数组"}}
        ),
    )

    with pytest.raises(InvalidExtractionCandidateError):
        extractor.extract([source])


@pytest.mark.unit
def test_dynamic_extractor_falls_back_to_inline_text_when_registry_misses():
    """registry 未配置或 key 缺失时，把 metadata 的值当文本直接用（兼容内联文本）。"""
    llm = _ScriptedLLM()
    fallback = _FallbackExtractor()
    source = _unit(
        "source-1",
        "原始内容",
        {"_extract_prompt_episodic": "直接当文本用的 prompt"},
    )
    extractor = DynamicLLMExtractor(llm, fallback)  # 无 registry

    result = extractor.extract([source])

    assert len(result) == 1
    assert llm.messages[0][0].content == "直接当文本用的 prompt"


# ----------------------------------------------------------------------
# PromptRegistry 测试
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_prompt_registry_resolves_by_phase_and_key():
    registry = PromptRegistry.from_dict(
        {
            "consolidate": {"episodic": "事件变化时替换旧记忆"},
            "reflect": {"default": "反思默认 prompt"},
        }
    )

    assert registry.get(PHASE_CONSOLIDATE, "episodic") == "事件变化时替换旧记忆"
    assert registry.get(PHASE_REFLECT, "default") == "反思默认 prompt"
    assert registry.get(PHASE_EXTRACT, "missing") is None
    assert registry.get(PHASE_CONSOLIDATE, "missing") is None


@pytest.mark.unit
def test_prompt_registry_empty_when_not_configured():
    registry = PromptRegistry.from_dict({})

    assert registry.get(PHASE_CONSOLIDATE, "any") is None
    assert registry.get(PHASE_REFLECT, "any") is None


# ----------------------------------------------------------------------
# DynamicEvolver 测试
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_dynamic_evolver_adds_candidate_when_no_hit():
    evolver, kv, index = _make_evolver()
    candidate = _unit("candidate", "新事实")

    result = evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert result.created_ids == ["candidate"]
    assert loads(kv.get(candidate.scope, memory_key(candidate.id))).content == "新事实"
    assert index.built == [candidate]


@pytest.mark.unit
def test_dynamic_evolver_supersedes_existing_via_llm_judge():
    existing = _unit("existing", "旧事实")
    kv = InMemoryKVStore()
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    storage = CompositeStorage(kv=kv, graph=InMemoryGraphStore())
    index = _Index(storage)
    extractor = _FallbackExtractor()
    dedup = _Dedup([(existing, 0.8)])
    registry = PromptRegistry.from_dict(
        {"consolidate": {"episodic": "事件变化时替换旧记忆"}}
    )
    llm = _ScriptedLLM(
        [json.dumps({"decision": "supersede", "existing_id": "existing", "reason": "new"})]
    )
    candidate = _unit(
        "candidate",
        "新事实",
        {
            "_extraction_strategy": "episodic",
            "_consolidation_prompt_episodic": "episodic",
        },
    )
    evolver = DynamicEvolver(
        extractor=extractor,
        abstractor=object(),
        associator=object(),
        index_builder=index,
        storage=storage,
        message_store=storage.kv,
        dedup=dedup,
        llm=llm,
        layer_annotator=None,
        prompt_registry=registry,
    )

    result = evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert result.created_ids == ["candidate"]
    assert result.superseded_ids == ["existing"]
    assert "事件变化时替换旧记忆" in llm.messages[0][0].content
    stored = loads(kv.get(candidate.scope, memory_key(candidate.id)))
    assert stored.system_metadata["dedup_decision"] == "supersede"


@pytest.mark.unit
def test_dynamic_evolver_update_empty_merge_falls_back_to_concatenation():
    """UPDATE 但合并结果为空串 → 视同合并失败，降级拼接新旧内容（Issue #189）。"""
    existing = _unit("existing", "旧事实")
    kv = InMemoryKVStore()
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    storage = CompositeStorage(kv=kv, graph=InMemoryGraphStore())
    index = _Index(storage)
    dedup = _Dedup([(existing, 0.8)])
    registry = PromptRegistry.from_dict({"consolidate": {"episodic": "巩固判定"}})
    llm = _ScriptedLLM(
        [
            json.dumps({"decision": "update", "existing_id": "existing", "reason": "补充"}),
            "",  # _merge_content 调用：LLM 200 但返回空串
        ]
    )
    candidate = _unit(
        "candidate",
        "新事实",
        {"_extraction_strategy": "episodic", "_consolidation_prompt_episodic": "episodic"},
    )
    evolver = DynamicEvolver(
        extractor=_FallbackExtractor(),
        abstractor=object(),
        associator=object(),
        index_builder=index,
        storage=storage,
        message_store=storage.kv,
        dedup=dedup,
        llm=llm,
        layer_annotator=None,
        prompt_registry=registry,
    )

    result = evolver.evolve([candidate], EvolveMode.EXTRACT)

    # UPDATE 照常执行，但 content 为降级拼接（非空串）
    assert result.updated_ids == ["existing"]
    assert result.created_ids == []
    kept = loads(kv.get(existing.scope, memory_key(existing.id)))
    assert kept.content == "旧事实\n新事实"


@pytest.mark.unit
def test_dynamic_evolver_invalid_llm_response_falls_back_to_add():
    evolver, kv, _ = _make_evolver(
        llm=_ScriptedLLM(["not-json"]),
        dedup_hits=[],
        prompts={"consolidate": {"custom": "自定义策略"}},
    )
    candidate = _unit(
        "candidate",
        "新事实",
        {"_consolidation_prompt_custom": "custom"},
    )

    result = evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert result.created_ids == ["candidate"]


@pytest.mark.unit
def test_dynamic_evolver_high_similarity_skips_llm_judge():
    existing = _unit("existing", "完全相同的记忆")
    evolver, kv, _ = _make_evolver(dedup_hits=[(existing, 0.95)])
    candidate = _unit("candidate", "完全相同的记忆")

    result = evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert result.created_ids == []
    assert result.superseded_ids == []
    # 高相似度 NOOP：候选不落盘
    with pytest.raises(Exception):
        kv.get(candidate.scope, memory_key(candidate.id))


@pytest.mark.unit
def test_dynamic_evolver_procedural_falls_back_to_parent():
    """procedural=true 走父类行为（不判定、直接落盘）。"""
    evolver, kv, _ = _make_evolver()
    candidate = _unit("candidate", "做了X", {"procedural": "true"})

    result = evolver.evolve([candidate], EvolveMode.EXTRACT)

    # procedural 路径：extractor 产 1 条直接落盘（不走 consolidate/reflect）
    assert len(result.created_ids) >= 1


# ----------------------------------------------------------------------
# 默认引擎：直写路径回归
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_default_engine_writes_through_without_consolidator():
    from jiuwen_memory.api.memory_api_impl import build_kernel

    kernel = build_kernel(
        config=Config.from_dict(
            {
                "security": {
                    "default": {"target": "local", "params": {"key_hex": _TEST_KEY_HEX}}
                }
            }
        )
    )
    scope = Scope(org="org", user="user")

    first = kernel.api.add("完全相同的记忆", scope, security=legacy_request_context(scope))
    second = kernel.api.add("完全相同的记忆", scope, security=legacy_request_context(scope))

    # 默认直写路径：两次都落盘，不去重（去重交给显式 evolve）
    assert len(first) == 1
    assert len(second) == 1


# ----------------------------------------------------------------------
# Producer 注册测试
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_evolver_producer_registers_dynamic():
    from jiuwen_memory.construction import bootstrap

    bootstrap.register_constructors()

    assert "dynamic" in EvolverProducer.known()
    assert "orchestrating" in EvolverProducer.known()
