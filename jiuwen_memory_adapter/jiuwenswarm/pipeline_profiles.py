# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Named document write profiles, shared by SDK and HTTP service composition."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

DOCUMENT_FACT = "document_fact"
TEAM_TURN = "team_turn"
TEAM_CONCLUSION = "team_conclusion"


def _component(config: dict, section: str, reference: str | dict, target: str) -> dict:
    raw = reference if isinstance(reference, dict) else config.get(section, {}).get(reference, {})
    result = deepcopy(raw) if isinstance(raw, dict) else {"target": raw}
    result["target"] = target
    result.setdefault("params", {})
    return result


def configure_document_pipelines(config: dict[str, Any]) -> dict[str, Any]:
    """Return a fresh config; preserve explicit named overrides and existing routes.

    Ordinary SDK providers call this after loading their baseline. HTTP deployments
    can use the same function before assembling the service runtime.
    """
    result = deepcopy(config)
    pipeline = result.setdefault("pipeline", {}).setdefault("default", {
        "target": "metadata", "params": {},
    })
    if not isinstance(pipeline, dict) or pipeline.get("target") != "metadata":
        raise ValueError("Document profiles require the metadata pipeline")
    params = pipeline.setdefault("params", {})
    if params.setdefault("route_key", "pipeline") != "pipeline":
        raise ValueError("Document profiles require route_key=pipeline")
    params.setdefault("strict_routes", True)
    fallback = params.setdefault("fallback", "default")
    profiles = params.setdefault("profiles", {})
    baseline = profiles.setdefault(fallback, {
        "index_builder": "default", "retriever": "default", "evolver": "default",
        "classifier": "default",
    })
    _configure_components(result, baseline)
    for name in (DOCUMENT_FACT, TEAM_TURN):
        profiles.setdefault(name, {**baseline, "evolver": name})
    profiles.setdefault(TEAM_CONCLUSION, {**baseline, "classifier": TEAM_CONCLUSION})
    return result


def _configure_components(config: dict, baseline: dict) -> None:
    evolver_name = baseline["evolver"]
    raw = config.get("evolver", {}).get(evolver_name, {})
    evolver = deepcopy(raw) if isinstance(raw, dict) else {"target": raw}
    evolver.setdefault("target", "orchestrating")
    params = evolver.setdefault("params", {})
    params.setdefault("index_builder", baseline["index_builder"])
    extractor_name = params.get("extractor", "default")
    dedup_name = params.get("dedup", "default")
    dedup = _component(config, "dedup", dedup_name, "project_team")
    config.setdefault("dedup", {}).setdefault("project_team", dedup)
    for name, target in ((DOCUMENT_FACT, "document_fact"), (TEAM_TURN, "team_llm")):
        extractor = _component(config, "extractor", extractor_name, target)
        config.setdefault("extractor", {}).setdefault(name, extractor)
        variant = deepcopy(evolver)
        variant["params"].update(extractor=name, dedup="project_team")
        config.setdefault("evolver", {}).setdefault(name, variant)
    classifier_params = {}
    if baseline.get("classifier"):
        classifier_params["base_classifier"] = baseline["classifier"]
    config.setdefault("classifier", {}).setdefault(TEAM_CONCLUSION, {
        "target": "team_document", "params": classifier_params,
    })
