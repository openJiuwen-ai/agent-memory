"""Schema extension of the official :mod:`composite_storage` assembly target.

The data-plane implementation remains :class:`CompositeStorage`; this module only
adds the ``schema_entities`` named vector/fulltext ports during configuration
assembly. Keeping the extension in a separate Producer avoids modifying the
official ``composite`` target.
"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import RetrievalPipeline
from jiuwen_memory.storage.fs import FsProducer
from jiuwen_memory.storage.fulltext import FulltextProducer
from jiuwen_memory.storage.fusion import FusionProducer
from jiuwen_memory.storage.graph import GraphProducer
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.storage import StorageProducer
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
from jiuwen_memory.storage.vector import VectorProducer

_SCHEMA_NAMED_PORTS = ("layers_l0", "layers_l1", "schema_entities")


def _optional_store(
    producer: type[Factory],
    config: Any,
    field: str,
    *,
    include_default: bool = False,
) -> Any | None:
    if field not in config.params:
        return producer.build("memory", {}, config.ctx) if include_default else None
    return producer.dep(config, field)


def _schema_named_ports(producer: type[Factory], config: Any) -> dict[str, Any]:
    namespace = config.ctx.namespaces.get(producer.TOP_NAME, {})
    return {
        name: producer.build_named(name, config.ctx)
        for name in _SCHEMA_NAMED_PORTS
        if name in namespace
    }


@StorageProducer.register("schema_composite")
def _build(config: Any) -> CompositeStorage:
    """Build the official CompositeStorage with Schema Entity search ports."""

    pipeline_value = config.get(
        "preferred_retrieval_pipeline",
        RetrievalPipeline.RECALL_GET_RANK.value,
    )
    try:
        preferred_pipeline = RetrievalPipeline(pipeline_value)
    except ValueError as exc:
        supported = [pipeline.value for pipeline in RetrievalPipeline]
        raise ValidationError(
            f"Unsupported preferred_retrieval_pipeline {pipeline_value!r}; "
            f"expected one of {supported}"
        ) from exc

    return CompositeStorage(
        kv=KvProducer.dep(config, default="memory"),
        vector=_optional_store(
            VectorProducer,
            config,
            "vector_store",
            include_default=config.get("__default_capabilities", False),
        ),
        fulltext=_optional_store(
            FulltextProducer,
            config,
            "fulltext_store",
            include_default=config.get("__default_capabilities", False),
        ),
        graph=_optional_store(
            GraphProducer,
            config,
            "graph_store",
            include_default=config.get("__default_capabilities", False),
        ),
        fusion=_optional_store(FusionProducer, config, "fusion_store"),
        fs=_optional_store(FsProducer, config, "fs_store"),
        vector_ports=_schema_named_ports(VectorProducer, config),
        fulltext_ports=_schema_named_ports(FulltextProducer, config),
        preferred_pipeline=preferred_pipeline,
    )
