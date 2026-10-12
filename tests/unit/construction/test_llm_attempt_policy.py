"""Construction LLM components share one total-attempts policy."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import ChatMessage
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.construction.abstractor import AbstractorProducer
from jiuwen_memory.construction.abstractor_impl.llm_abstractor import LLMAbstractor
from jiuwen_memory.construction.associator import AssociatorProducer
from jiuwen_memory.construction.associator_impl.llm_associator import LLMAssociator
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.classifier import ClassifierProducer
from jiuwen_memory.construction.classifier_impl.llm_classifier import LLMClassifier
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.extractor_impl.llm_extractor import ExtractorImpl
from jiuwen_memory.construction.layer_annotator import LayerAnnotatorProducer
from jiuwen_memory.construction.layer_annotator_impl.llm_layer_annotator import (
    LLMLayerAnnotator,
)
from jiuwen_memory.construction.router import RouterProducer, parse_route_table
from jiuwen_memory.construction.router_impl.llm_router import LLMRouter
from tests.unit.construction.fixtures import HashEmbedder, RuleFeatureExtractor


class _FailingLLM(LLM):
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del messages, options
        self.calls += 1
        raise self.error


def _router_table():
    return parse_route_table(
        {
            "coord_entities": [],
            "memory_classes": [
                {
                    "name": "fallback",
                    "owner": "user",
                    "space_template": "u_{user}",
                    "fallback": True,
                }
            ],
            "narrow_dims": [],
        }
    )


def _component_factories(
    llm: LLM, max_attempts: int | object, retry_backoff_ms: int | object = 0,
) -> list[Callable[[], object]]:
    features = RuleFeatureExtractor()
    embedder = HashEmbedder(dim=8)
    return [
        lambda: LLMClassifier(
            llm, max_attempts=max_attempts, retry_backoff_ms=retry_backoff_ms,
        ),
        lambda: ExtractorImpl(
            llm, max_attempts=max_attempts, retry_backoff_ms=retry_backoff_ms,
        ),
        lambda: LLMAbstractor(
            llm,
            features,
            max_attempts=max_attempts,
            retry_backoff_ms=retry_backoff_ms,
        ),
        lambda: LLMAssociator(
            llm,
            features,
            embedder,
            deep_discovery=False,
            max_attempts=max_attempts,
            retry_backoff_ms=retry_backoff_ms,
        ),
        lambda: LLMLayerAnnotator(
            llm, max_attempts=max_attempts, retry_backoff_ms=retry_backoff_ms,
        ),
        lambda: LLMRouter(
            llm,
            _router_table(),
            max_attempts=max_attempts,
            retry_backoff_ms=retry_backoff_ms,
        ),
    ]


@pytest.mark.parametrize("component_index", range(6))
def test_each_llm_component_uses_max_attempts_as_total_calls(component_index: int) -> None:
    error = RuntimeError("down")
    llm = _FailingLLM(error)
    component = _component_factories(llm, max_attempts=2)[component_index]()

    with pytest.raises(RuntimeError, match="down") as raised:
        getattr(component, "_call_llm_with_retry")([])

    assert raised.value is error
    assert llm.calls == 2


@pytest.mark.parametrize("component_index", range(6))
def test_each_llm_component_attempts_once_when_max_attempts_is_one(
    component_index: int,
) -> None:
    llm = _FailingLLM(RuntimeError("down"))
    component = _component_factories(llm, max_attempts=1)[component_index]()

    with pytest.raises(RuntimeError, match="down"):
        getattr(component, "_call_llm_with_retry")([])

    assert llm.calls == 1


@pytest.mark.parametrize("component_index", range(6))
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "bad"])
def test_each_llm_component_rejects_invalid_max_attempts(
    component_index: int, value: object,
) -> None:
    llm = _FailingLLM(RuntimeError("down"))
    with pytest.raises(ValidationError, match="max_attempts"):
        _component_factories(llm, max_attempts=value)[component_index]()


@pytest.mark.parametrize("component_index", range(6))
@pytest.mark.parametrize("value", [-1, True, 1.5, "bad"])
def test_each_llm_component_rejects_invalid_backoff(
    component_index: int, value: object,
) -> None:
    llm = _FailingLLM(RuntimeError("down"))
    factory = _component_factories(llm, max_attempts=1, retry_backoff_ms=value)[
        component_index
    ]
    with pytest.raises(ValidationError, match="retry_backoff_ms"):
        factory()


@pytest.mark.parametrize(
    ("producer", "target", "old_key", "new_key"),
    [
        (ExtractorProducer, "llm", "extractor_retry_max", "extractor_max_attempts"),
        (ExtractorProducer, "dynamic_llm", "extractor_retry_max", "extractor_max_attempts"),
        (
            ExtractorProducer,
            "entity_schema",
            "extractor_retry_max",
            "extractor_max_attempts",
        ),
        (AbstractorProducer, "llm", "abstractor_retry_max", "abstractor_max_attempts"),
        (AssociatorProducer, "llm", "associator_retry_max", "associator_max_attempts"),
        (ClassifierProducer, "llm", "classifier_retry_max", "classifier_max_attempts"),
        (
            LayerAnnotatorProducer,
            "llm",
            "layer_annotator_retry_max",
            "layer_annotator_max_attempts",
        ),
        (RouterProducer, "llm", "retry_max_retries", "router_max_attempts"),
        (RouterProducer, "llm", "retry_backoff_ms", "router_retry_backoff"),
    ],
)
def test_build_rejects_deprecated_llm_attempt_keys(
    producer: object,
    target: str,
    old_key: str,
    new_key: str,
) -> None:
    register_constructors()

    with pytest.raises(
        ValidationError,
        match=rf"{old_key}.*{new_key}",
    ):
        producer.build(target, {old_key: 5}, AssemblyContext())


def test_build_rejects_deprecated_global_llm_attempt_key() -> None:
    register_constructors()

    with pytest.raises(ValidationError, match="extractor_retry_max.*extractor_max_attempts"):
        ExtractorProducer.build("llm", {}, AssemblyContext(globals={"extractor_retry_max": 5}))
