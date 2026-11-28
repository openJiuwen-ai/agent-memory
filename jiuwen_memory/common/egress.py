# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Data-destination declarations for memory components (F17 D9).

A component that sends memory content off the machine must declare what it sends.
The inventory is fail-closed: an undeclared component is reported as "unknown
destination", never as "no outbound content". Declarations live next to the
component's registration and are keyed by its ``target`` name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# Content kinds a component may send.
QUERY = "query"
CANDIDATES = "document"
METADATA = "metadata"
ALL_KINDS = frozenset({QUERY, CANDIDATES, METADATA})


@dataclass(frozen=True)
class EgressDeclaration:
    target: str
    remote: bool
    sends: frozenset[str] = frozenset()
    endpoint_param: str = ""
    note: str = ""


@dataclass(frozen=True)
class EgressRow:
    component: str
    target: str
    destination: str
    sends: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "target": self.target,
            "destination": self.destination,
            "sends": list(self.sends),
            "note": self.note,
        }


_DECLARATIONS: dict[str, EgressDeclaration] = {}
# Storage/control namespaces are not content egress surfaces. Any other configured
# component category is included even without a declaration, so the page can show
# "unknown" instead of silently omitting a plugin category.
_LOCAL_ONLY_NAMESPACES = frozenset(
    {
        "globals", "local", "http", "server", "security", "authenticator",
        "authorizer", "binding_policy", "kv_store", "vector_store",
        "fulltext_store", "graph_store", "markdown_store", "shadow_index",
        "permission", "audit", "grant_store", "delegation_store", "store_manager",
        "watchdog", "continuity", "recaller", "context", "memory", "tokenizer",
        "cache", "logging",
    }
)

# First-party implementations of the template's build/control categories. They run in
# this process: content only leaves through an ``llm``/``embedder``/``reranker``
# component, which declares its own destination. Declarations are per target, never per
# namespace, so a third-party plugin registered under the same category stays "unknown".
_BUILTIN_LOCAL: dict[str, frozenset[str]] = {
    "abstractor": frozenset({"concat", "llm", "skill"}),
    "associator": frozenset({"evidence_llm", "keyword", "llm"}),
    "chunker": frozenset({"fixed_window", "recursive"}),
    "classifier": frozenset({"keyword", "llm"}),
    "config_source": frozenset({"dict", "overlay", "yaml_defaults"}),
    "constructor": frozenset({"document"}),
    "dedup": frozenset({"keyword", "project_team", "shadow", "vector"}),
    "discloser": frozenset({"structured", "truncating"}),
    "engine": frozenset({"agent", "cloud", "in_memory"}),
    "evolver": frozenset({"dynamic", "orchestrating", "schema_orchestrating"}),
    "extractor": frozenset(
        {
            "agent_llm", "document_fact", "dynamic_llm", "entity_schema", "experience",
            "keyword", "llm", "team_llm", "video_memory",
        }
    ),
    "feature_extractor": frozenset({"hanlp", "keyword", "spacy"}),
    "fuser": frozenset({"BM25_scored_fuser", "rrf", "score_max", "weighted_rrf"}),
    "governor": frozenset({"in_memory"}),
    "ingestor": frozenset({"simple"}),
    "job_factory": frozenset({"default"}),
    "lifecycle": frozenset({"kv"}),
    "normalizer": frozenset({"passthrough", "routing", "video"}),
    "policy": frozenset({"dict", "loopback"}),
    "query_parser": frozenset({"simple"}),
    "retriever": frozenset({"multimodal", "pipeline"}),
    "scheduler": frozenset({"async_timer", "in_process"}),
    "space": frozenset({"kv"}),
}

_BUILTIN_NOTE = "runs in this process; model calls go through llm/embedder"


def declare(
    namespace: str,
    target: str,
    *,
    remote: bool,
    sends: Iterable[str] = (),
    endpoint_param: str = "",
    note: str = "",
) -> None:
    """Register a component's declaration (called next to ``@XProducer.register``)."""
    kinds = frozenset(sends)
    unknown = kinds - ALL_KINDS
    if unknown:
        raise ValueError(f"unknown egress kinds: {sorted(unknown)}")
    if remote and not kinds:
        raise ValueError("a remote component must declare what it sends")
    if not remote and kinds:
        raise ValueError("a local component must not declare outbound content")
    _DECLARATIONS[f"{namespace}:{target}"] = EgressDeclaration(
        target=target, remote=remote, sends=kinds, endpoint_param=endpoint_param, note=note
    )


def declaration_for(namespace: str, target: str) -> EgressDeclaration | None:
    return _DECLARATIONS.get(f"{namespace}:{target}")


def inventory(
    components: Iterable[tuple[str, str, str]],
    *,
    values: Mapping[str, str] | None = None,
    overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> list[EgressRow]:
    """Render the report for ``(component name, target)`` pairs.

    ``values`` resolves endpoint parameters from the effective configuration; a remote
    component without a resolvable endpoint still reads as remote, not as local.
    """
    resolved = dict(values or {})
    per_component = dict(overrides or {})
    rows: list[EgressRow] = []
    for name, namespace, target in components:
        declaration = _DECLARATIONS.get(f"{namespace}:{target}")
        if declaration is None and target in _BUILTIN_LOCAL.get(namespace, frozenset()):
            declaration = EgressDeclaration(
                target=target, remote=False, note=_BUILTIN_NOTE
            )
        if declaration is None:
            rows.append(
                EgressRow(
                    component=name,
                    target=target,
                    destination="unknown",
                    note="destination not declared by this component",
                )
            )
            continue
        if not declaration.remote:
            rows.append(
                EgressRow(
                    component=name,
                    target=target,
                    destination="local",
                    note=declaration.note or "runs in this process",
                )
            )
            continue
        endpoint = ""
        if declaration.endpoint_param:
            endpoint = per_component.get(name, {}).get(
                declaration.endpoint_param, ""
            ) or resolved.get(declaration.endpoint_param, "")
        rows.append(
            EgressRow(
                component=name,
                target=target,
                destination=endpoint or "unknown",
                sends=sorted(declaration.sends),
                note=declaration.note or (
                    "endpoint could not be resolved from the configuration"
                    if declaration.endpoint_param and not endpoint else ""
                ),
            )
        )
    return rows


class EgressInventory:
    """Report of every configured component and where it sends content."""

    def __init__(self, rows: list[EgressRow]) -> None:
        self.rows = rows

    @classmethod
    def from_settings(cls, memory_api: Mapping | None) -> EgressInventory:
        settings = dict(memory_api or {})
        values = {
            str(key): str(value)
            for key, value in (settings.get("globals") or {}).items()
            if isinstance(value, (str, int, float, bool))
        }
        components: list[tuple[str, str, str]] = []
        overrides: dict[str, dict[str, str]] = {}
        # Cover every declared namespace and every configured plugin category not in
        # the local storage/control set: an undeclared category must read as unknown,
        # not disappear from the report.
        namespaces = sorted(
            {key.split(":", 1)[0] for key in _DECLARATIONS}
            | {
                str(name)
                for name, value in settings.items()
                if name not in _LOCAL_ONLY_NAMESPACES and isinstance(value, dict)
            }
        )
        for namespace in namespaces:
            instances = settings.get(namespace) or {}
            if not isinstance(instances, dict):
                continue
            for name, spec in instances.items():
                if isinstance(spec, str):
                    target, params = spec, {}
                elif isinstance(spec, dict):
                    target = spec.get("target", "")
                    params = spec.get("params") or {}
                else:
                    continue
                if not target:
                    continue
                component = f"{namespace}.{name}"
                components.append((component, namespace, str(target)))
                overrides[component] = {
                    str(key): str(value)
                    for key, value in params.items()
                    if isinstance(value, (str, int, float, bool))
                }
        return cls(inventory(components, values=values, overrides=overrides))

    def as_list(self) -> list[dict]:
        return [row.as_dict() for row in self.rows]
