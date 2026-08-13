from __future__ import annotations

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult, RetrievedItem

_INDEX_BUILDERS: dict[str, "RecordingIndexBuilder"] = {}


class RecordingIndexBuilder(IndexBuilder):
    def __init__(self, name: str) -> None:
        self.name = name
        self.built: list[str] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units) -> None:
        self.built.extend(unit.content for unit in units)

    def update(self, units) -> None:
        return None

    def remove(self, units) -> None:
        return None

    def rebuild(self) -> None:
        return None


class NamedRetriever(Retriever):
    def __init__(self, name: str) -> None:
        self.name = name

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        return RetrievalResult(items=[RetrievedItem(unit_id=self.name, content=query.text)])


@IndexBuilderProducer.register("recording")
def _build_recording_index(config):
    name = config.get("name", config.name)
    index_builder = RecordingIndexBuilder(name)
    _INDEX_BUILDERS[name] = index_builder
    return index_builder


@RetrieverProducer.register("named")
def _build_named_retriever(config):
    return NamedRetriever(config.get("name", config.name))


def _kernel_config() -> Config:
    return Config.from_dict(
        {
            "constructor": {
                "default": {"target": "recording", "params": {"name": "default"}},
                "coding": {"target": "recording", "params": {"name": "coding"}},
            },
            "retriever": {
                "default": {"target": "named", "params": {"name": "default"}},
                "coding": {"target": "named", "params": {"name": "coding"}},
            },
            "classifier": {"default": {"target": "keyword"}},
            "pipeline": {
                "default": {
                    "target": "metadata",
                    "params": {
                        "route_key": "memory_type",
                        "fallback": "default",
                        "routes": {"coding": "coding"},
                        "profiles": {
                            "default": {
                                "index_builder": "default",
                                "retriever": "default",
                                "evolver": "default",
                                "classifier": "default",
                            },
                            "coding": {
                                "index_builder": "coding",
                                "retriever": "coding",
                                "evolver": "default",
                                "classifier": "default",
                            },
                        },
                    },
                }
            },
        }
    )


def test_engine_write_uses_pipeline_profile_from_memory_type() -> None:
    _INDEX_BUILDERS.clear()
    kernel = build_kernel(config=_kernel_config())
    scope = Scope(user="u1")

    kernel.api.add(
        "use pytest for this repo",
        scope,
        identity=scope,
        metadata={"memory_type": "coding"},
    )

    assert _INDEX_BUILDERS["default"].built == []
    assert _INDEX_BUILDERS["coding"].built == ["use pytest for this repo"]


def test_engine_recall_uses_pipeline_profile_from_context_extensions() -> None:
    kernel = build_kernel(config=_kernel_config())
    scope = Scope(user="u1")

    result = kernel.api.search(
        "test strategy",
        Context(scope=scope, extensions={"memory_type": "coding"}),
        identity=scope,
    )

    assert [item.unit_id for item in result.items] == ["coding"]


def test_engine_recall_uses_pipeline_profile_from_metadata_memory_type_filter() -> None:
    kernel = build_kernel(config=_kernel_config())
    scope = Scope(user="u1")

    result = kernel.api.search(
        "test strategy",
        Context(scope=scope),
        identity=scope,
        filters={"metadata.memory_type": "coding"},
    )

    assert [item.unit_id for item in result.items] == ["coding"]


def test_engine_recall_canonicalizes_legacy_memory_type_filter_name() -> None:
    kernel = build_kernel(config=_kernel_config())
    scope = Scope(user="u1")

    result = kernel.api.search(
        "test strategy",
        Context(scope=scope),
        identity=scope,
        filters={"memory_type": "coding"},
    )

    assert [item.unit_id for item in result.items] == ["coding"]
