from __future__ import annotations

import json
import re
from xml.etree import ElementTree

import pytest

from api.memory_api_impl import build_kernel
from common.base import PluginType
from common.llm.base import LLM
from common.type_def import MemoryTier, MemoryUnit, Modality, Scope, Segment, memory_key
from common.type_def.memory_codec import dumps, loads
from construction.base import OperatorType
from construction.consolidation_impl.consolidation_1 import Consolidation1
from construction.consolidation_impl.consolidation_2 import Consolidation2
from construction.extractor import Extractor
from construction.extractor_impl.dynamic_llm_extractor import DynamicLLMExtractor
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore


class _ScriptedLLM(LLM):
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.messages = []

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
    def parse_response(
        self,
        response: str,
        sources: list[MemoryUnit],
        strategy: str,
    ) -> list[MemoryUnit]:
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
                    metadata={"parser_format": "xml"},
                )
            )
        return result


class _FailingStrategyExtractor(DynamicLLMExtractor):
    def parse_response(
        self,
        response: str,
        sources: list[MemoryUnit],
        strategy: str,
    ) -> list[MemoryUnit]:
        if strategy == "broken":
            raise ValueError("broken strategy")
        return super().parse_response(response, sources, strategy)


class _Dedup:
    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])

    def recall(self, candidate):
        return list(self.hits)


class _Index:
    def __init__(self) -> None:
        self.built = []
        self.updated = []

    def build(self, units):
        self.built.extend(units)

    def update(self, units):
        self.updated.extend(units)


def _unit(unit_id: str, content: str, metadata=None) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="org", user="user"),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
        metadata=dict(metadata or {}),
    )


@pytest.mark.unit
def test_dynamic_extractor_runs_each_custom_strategy_and_keeps_consolidation_prompt():
    llm = _ScriptedLLM()
    fallback = _FallbackExtractor()
    source = _unit(
        "source-1",
        "原始内容",
        {
            "_extract_prompt_episodic": "只抽取事件",
            "_extract_prompt_custom": "只抽取自定义事实",
            "_consolidation_prompt_episodic": "按事件时序巩固",
        },
    )
    extractor = DynamicLLMExtractor(llm, fallback)

    result = extractor.extract([source])

    assert len(result) == 2
    assert [unit.metadata["_extraction_strategy"] for unit in result] == [
        "episodic",
        "custom",
    ]
    assert all(
        unit.metadata["_consolidation_prompt_episodic"] == "按事件时序巩固"
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
            "_extract_prompt_xml": (
                "按 XML 格式抽取："
                '<memories><memory source_id="...">...</memory></memories>'
            ),
            "_consolidation_prompt_xml": "按 XML 策略巩固",
        },
    )

    result = _XmlDynamicExtractor(llm, fallback).extract([source])

    assert len(result) == 1
    assert isinstance(result[0], MemoryUnit)
    assert result[0].content == "XML抽取结果"
    assert result[0].provenance == ["source-1"]
    assert result[0].metadata["parser_format"] == "xml"
    assert result[0].metadata["_extraction_strategy"] == "xml"
    assert result[0].metadata["_consolidation_prompt_xml"] == "按 XML 策略巩固"
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
            "_extract_prompt_broken": "无法解析的策略",
            "_extract_prompt_json": "正常 JSON 策略",
        },
    )

    result = _FailingStrategyExtractor(_ScriptedLLM(), _FallbackExtractor()).extract([source])

    assert len(result) == 1
    assert result[0].metadata["_extraction_strategy"] == "json"


@pytest.mark.unit
def test_consolidation_1_adds_candidate_when_no_hit():
    kv = InMemoryKVStore()
    index = _Index()
    candidate = _unit("candidate", "新事实")
    consolidator = Consolidation1(_Dedup(), index, kv, _ScriptedLLM())

    result = consolidator.consolidate([candidate])

    assert result.created_ids == ["candidate"]
    assert loads(kv.get(candidate.scope, memory_key(candidate.id))).content == "新事实"
    assert index.built == [candidate]


@pytest.mark.unit
def test_consolidation_2_uses_matching_strategy_and_fixed_contract():
    kv = InMemoryKVStore()
    index = _Index()
    existing = _unit("existing", "旧事实")
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    candidate = _unit(
        "candidate",
        "新事实",
        {
            "_extraction_strategy": "episodic",
            "_consolidation_prompt_episodic": "事件变化时替换旧记忆",
            "_consolidation_prompt_custom": "自定义策略",
        },
    )
    llm = _ScriptedLLM(
        [json.dumps({"decision": "supersede", "existing_id": "existing", "reason": "new"})]
    )
    consolidator = Consolidation2(_Dedup([(existing, 0.8)]), index, kv, llm)

    result = consolidator.consolidate([candidate])

    assert result.created_ids == ["candidate"]
    assert result.superseded_ids == ["existing"]
    assert "事件变化时替换旧记忆" in llm.messages[0][0].content
    assert "Mandatory output contract" in llm.messages[0][0].content
    stored = loads(kv.get(candidate.scope, memory_key(candidate.id)))
    assert stored.metadata["_consolidation_strategy"] == "episodic"


@pytest.mark.unit
def test_consolidation_2_invalid_response_falls_back_to_consolidation_1():
    kv = InMemoryKVStore()
    index = _Index()
    candidate = _unit(
        "candidate",
        "新事实",
        {"_consolidation_prompt_custom": "自定义策略"},
    )
    consolidator = Consolidation2(_Dedup(), index, kv, _ScriptedLLM(["not-json"]))

    result = consolidator.consolidate([candidate])

    assert result.created_ids == ["candidate"]


@pytest.mark.unit
def test_default_engine_routes_plain_write_through_consolidation_2():
    kernel = build_kernel()
    scope = Scope(org="org", user="user")

    first = kernel.api.write("完全相同的记忆", scope, identity=scope)
    second = kernel.api.write("完全相同的记忆", scope, identity=scope)

    assert len(first) == 1
    assert second == []
