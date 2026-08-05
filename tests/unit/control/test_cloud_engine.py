from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from common.errors import ValidationError
from common.type_def import (
    FilterClause,
    FilterOp,
    MemoryUnit,
    Modality,
    RawPayload,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from common.type_def.memory_codec import dumps
from construction.base import OperatorType
from construction.classifier import Classifier
from construction.evolver import EvolveMode, Evolver, EvolveResult
from construction.index_builder import IndexBuilder
from control.base import ControlOperatorType
from control.engine_impl.cloud_engine import CloudEngine
from control.lifecycle import LifecycleManager
from control.pipeline import MemoryPipeline, PipelineBinding
from control.scheduler_impl.in_process_scheduler import InProcessScheduler
from control.types import BatchWriteItem, DeleteSelector, MemoryPatch, UpdateMode
from ingest.base import IngestOperatorType
from ingest.ingestor import Ingestor
from retrieval.base import RetrievalOperatorType
from retrieval.retriever import Retriever
from retrieval.types import RetrievalQuery, RetrievalResult, RetrievedItem
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


class _RecordingIngestor(Ingestor):
    def operator_type(self) -> IngestOperatorType:
        return IngestOperatorType.INGESTOR

    def health(self) -> None:
        return None

    def ingest(self, payloads: list[RawPayload]) -> list[MemoryUnit]:
        units: list[MemoryUnit] = []
        now = datetime.now(timezone.utc)
        for payload in payloads:
            units.append(
                MemoryUnit(
                    id=payload.id,
                    scope=payload.scope,
                    segments=[
                        Segment(
                            content=payload.data.decode("utf-8"),
                            source=payload.modality,
                        )
                    ],
                    temporal=Temporal(
                        t_event=payload.occurred_at or now,
                        t_ingest=now,
                        t_valid=now,
                    ),
                    metadata=dict(payload.metadata),
                )
            )
        return units


class _RecordingIndexBuilder(IndexBuilder):
    def __init__(self, name: str) -> None:
        self.name = name
        self.built: list[str] = []
        self.updated: list[str] = []
        self.removed: list[str] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit]) -> None:
        self.built.extend(unit.content for unit in units)

    def update(self, units: list[MemoryUnit]) -> None:
        self.updated.extend(unit.id for unit in units)

    def remove(self, units: list[MemoryUnit]) -> None:
        self.removed.extend(unit.id for unit in units)

    def rebuild(self) -> None:
        return None


class _RecordingClassifier(Classifier):
    def __init__(self, name: str) -> None:
        self.name = name
        self.classified: list[str] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.CLASSIFIER

    def health(self) -> None:
        return None

    def classify(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        for unit in units:
            unit.metadata["classified_by"] = self.name
            self.classified.append(unit.content)
        return units


class _RecordingRetriever(Retriever):
    def __init__(self, name: str) -> None:
        self.name = name
        self.queries: list[str] = []

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        self.queries.append(query.extensions.get("message_type", ""))
        return RetrievalResult(items=[RetrievedItem(unit_id=self.name, content=query.text)])


class _RecordingEvolver(Evolver):
    def __init__(self, name: str, kv: InMemoryKVStore) -> None:
        self.name = name
        self.kv = kv
        self.calls: list[tuple[list[str], EvolveMode]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append(([unit.content for unit in units], mode))
        created_ids: list[str] = []
        for unit in units:
            derived = MemoryUnit(
                id=f"{self.name}-derived-{len(created_ids)}",
                scope=unit.scope,
                segments=[Segment(content=f"derived:{unit.content}", source=unit.source)],
                temporal=unit.temporal,
                provenance=[unit.id],
                metadata=dict(unit.metadata),
            )
            self.kv.insert(unit.scope, memory_key(derived.id), dumps(derived))
            created_ids.append(derived.id)
        return EvolveResult(created_ids=created_ids)


class _RecordingKVStore(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.list_calls = []

    def list(self, scope, **kwargs):
        self.list_calls.append((scope, kwargs))
        return super().list(scope, **kwargs)


class _NoopLifecycle(LifecycleManager):
    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, scope, unit_ids, target) -> None:
        return None

    def supersede(self, scope, unit_id, invalid_at):
        raise AssertionError("not used in these tests")

    def sweep(self):
        return []


class _MessageTypePipeline(MemoryPipeline):
    def __init__(self, profiles: dict[str, PipelineBinding]) -> None:
        self.profiles = profiles

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.PIPELINE

    def health(self) -> None:
        return None

    def select_for_write(self, units: list[MemoryUnit]) -> PipelineBinding:
        route = units[0].metadata.get("message_type", "chat")
        return self.profiles.get(route, self.profiles["chat"])

    def select_for_recall(self, query: RetrievalQuery) -> PipelineBinding:
        route = query.extensions.get("message_type", "chat")
        return self.profiles.get(route, self.profiles["chat"])


def _engine():
    kv = _RecordingKVStore()
    chat_index = _RecordingIndexBuilder("chat")
    coding_index = _RecordingIndexBuilder("coding")
    chat_classifier = _RecordingClassifier("chat")
    coding_classifier = _RecordingClassifier("coding")
    chat_retriever = _RecordingRetriever("chat")
    coding_retriever = _RecordingRetriever("coding")
    chat_evolver = _RecordingEvolver("chat", kv)
    coding_evolver = _RecordingEvolver("coding", kv)
    profiles = {
        "chat": PipelineBinding(
            name="chat",
            index_builder=chat_index,
            retriever=chat_retriever,
            evolver=chat_evolver,
            classifier=chat_classifier,
        ),
        "coding": PipelineBinding(
            name="coding",
            index_builder=coding_index,
            retriever=coding_retriever,
            evolver=coding_evolver,
            classifier=coding_classifier,
        ),
    }
    return (
        CloudEngine(
            ingestor=_RecordingIngestor(),
            index_builder=chat_index,
            retriever=chat_retriever,
            kv=kv,
            scheduler=InProcessScheduler(),
            evolver=chat_evolver,
            lifecycle=_NoopLifecycle(),
            classifier=chat_classifier,
            pipeline=_MessageTypePipeline(profiles),
            default_message_type="chat",
            default_pipeline_name="chat",
        ),
        {
            "kv": kv,
            "chat_index": chat_index,
            "coding_index": coding_index,
            "chat_classifier": chat_classifier,
            "coding_classifier": coding_classifier,
            "chat_retriever": chat_retriever,
            "coding_retriever": coding_retriever,
            "chat_evolver": chat_evolver,
            "coding_evolver": coding_evolver,
        },
    )


def test_cloud_engine_write_routes_by_message_type_and_stamps_metadata() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(
        engine.write(
            "use pytest for this repo",
            scope,
            source=Modality.CODE,
            metadata={"message_type": "coding", "memory_type": "procedural"},
        )
    )

    assert records["chat_index"].built == []
    assert records["coding_index"].built == ["use pytest for this repo"]
    assert records["coding_classifier"].classified == ["use pytest for this repo"]
    assert units[0].metadata["message_type"] == "coding"
    assert units[0].metadata["pipeline"] == "coding"
    assert units[0].metadata["classified_by"] == "coding"

    context = asyncio.run(engine.permission_context_for_unit(units[0].id, scope))

    assert context.pipeline == "coding"
    assert context.memory_type == "procedural"
    assert context.metadata["message_type"] == "coding"


def test_cloud_engine_write_defaults_to_chat_message_type() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(engine.write("remember my meeting notes", scope))

    assert records["chat_index"].built == ["remember my meeting notes"]
    assert records["coding_index"].built == []
    assert units[0].metadata["message_type"] == "chat"
    assert units[0].metadata["pipeline"] == "chat"


def test_cloud_engine_batch_write_preserves_order_and_routes_each_item() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    result = asyncio.run(
        engine.batch_write(
            [
                BatchWriteItem(content="chat note", scope=scope, source=Modality.TEXT),
                BatchWriteItem(
                    content="coding note",
                    scope=scope,
                    source=Modality.CODE,
                    metadata={"message_type": "coding"},
                ),
            ]
        )
    )

    assert [outcome.units[0].content for outcome in result.outcomes] == ["chat note", "coding note"]
    assert records["chat_index"].built == ["chat note"]
    assert records["coding_index"].built == ["coding note"]
    assert result.outcomes[1].units[0].metadata["pipeline"] == "coding"


def test_cloud_engine_batch_write_collects_unexpected_error_and_skips_after_failure() -> None:
    engine, _ = _engine()
    scope = Scope(org="acme", user="alice")

    async def _raise_unexpected(*_args, **_kwargs):
        raise RuntimeError("unavailable dependency")

    engine.write = _raise_unexpected  # type: ignore[method-assign]
    result = asyncio.run(
        engine.batch_write(
            [BatchWriteItem(content="first", scope=scope), BatchWriteItem(content="second", scope=scope)],
            continue_on_error=False,
        )
    )

    assert [outcome.error_type for outcome in result.outcomes] == ["InternalError", "Skipped"]
    assert result.outcomes[0].error == "unexpected batch write failure"


def test_cloud_engine_recall_routes_by_message_type_extension() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    result = asyncio.run(
        engine.recall(scope, RetrievalQuery(text="testing", extensions={"message_type": "coding"}))
    )

    assert [item.unit_id for item in result.items] == ["coding"]
    assert records["coding_retriever"].queries == ["coding"]
    assert records["chat_retriever"].queries == []


def test_cloud_engine_list_forwards_query_and_returns_total_count() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", space="coding", user="alice")
    first = asyncio.run(
        engine.write(
            "first alpha memory",
            scope,
            metadata={"memory_type": "coding", "project": "alpha"},
        )
    )[0]
    second = asyncio.run(
        engine.write(
            "second alpha memory",
            scope,
            metadata={"memory_type": "coding", "project": "alpha"},
        )
    )[0]
    asyncio.run(
        engine.write(
            "beta memory",
            scope,
            metadata={"memory_type": "coding", "project": "beta"},
        )
    )
    filters = FilterClause("metadata.project", FilterOp.EQ, "alpha")
    extensions = {"vendor_mode": "strict"}

    result = asyncio.run(
        engine.list(
            scope,
            offset=1,
            limit=1,
            memory_types=["coding"],
            filters=filters,
            extensions=extensions,
        )
    )

    assert result.count == 2
    assert len(result.items) == 1
    assert result.items[0].id in {first.id, second.id}
    call_scope, call_options = records["kv"].list_calls[0]
    assert call_scope == scope
    assert call_options["offset"] == 1
    assert call_options["limit"] == 1
    assert call_options["memory_types"] == ["coding"]
    assert call_options["filters"] is filters
    assert call_options["extensions"] is extensions


def test_cloud_engine_infer_uses_profile_evolver_and_returns_derived_units() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(
        engine.write(
            "extract coding preference",
            scope,
            metadata={"message_type": "coding", "infer": "true"},
        )
    )

    assert records["coding_evolver"].calls == [(["extract coding preference"], EvolveMode.EXTRACT)]
    assert records["chat_evolver"].calls == []
    assert units[0].id == "coding-derived-0"
    assert units[0].content == "derived:extract coding preference"
    assert units[0].metadata["pipeline"] == "coding"
    assert units[0].metadata["message_type"] == "coding"


def test_cloud_engine_overwrite_moves_unit_between_profile_indexes() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")
    units = asyncio.run(engine.write("chat note", scope))

    updated = asyncio.run(
        engine.update(
            units[0].id,
            scope,
            MemoryPatch(
                content="coding note",
                metadata={"message_type": "coding"},
                mode=UpdateMode.OVERWRITE,
            ),
        )
    )

    assert updated.id == units[0].id
    assert updated.metadata["message_type"] == "coding"
    assert updated.metadata["pipeline"] == "coding"
    assert records["chat_index"].removed == [units[0].id]
    assert records["coding_index"].built == ["coding note"]


def test_cloud_engine_delete_rejects_empty_selector() -> None:
    engine, _ = _engine()

    try:
        asyncio.run(engine.delete(DeleteSelector()))
    except ValidationError:
        return
    else:
        raise AssertionError("empty selector should raise ValidationError")
