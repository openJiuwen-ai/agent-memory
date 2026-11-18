# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Document profile composition preserves host configuration and named overrides."""
from copy import deepcopy
from pathlib import Path

import yaml

from jiuwen_memory_adapter.jiuwenswarm.pipeline_profiles import configure_document_pipelines


def test_profiles_preserve_existing_routes_and_component_parameters():
    baseline = {"index_builder": "documents", "retriever": "search", "evolver": "facts",
                "classifier": "labels"}
    config = {
        "pipeline": {"default": {"target": "metadata", "params": {
            "route_key": "pipeline", "fallback": "base",
            "profiles": {"base": baseline, "video": {**baseline, "evolver": "video"}},
        }}},
        "evolver": {"facts": {"target": "orchestrating", "params": {
            "extractor": "facts", "dedup": "existing", "index_builder": "documents",
        }}},
        "extractor": {"facts": {"target": "agent_llm", "params": {"llm": "host_model"}}},
        "dedup": {"existing": {"target": "shadow", "params": {"embedder": "host_embedder"}}},
    }
    original = deepcopy(config)
    result = configure_document_pipelines(config)
    assert config == original
    assert configure_document_pipelines(result) == result
    profiles = result["pipeline"]["default"]["params"]["profiles"]
    assert profiles["video"] == original["pipeline"]["default"]["params"]["profiles"]["video"]
    assert profiles["document_fact"]["retriever"] == "search"
    assert result["extractor"]["team_turn"]["params"]["llm"] == "host_model"
    assert result["dedup"]["project_team"]["params"]["embedder"] == "host_embedder"
    assert result["classifier"]["team_conclusion"]["params"]["base_classifier"] == "labels"
    result["extractor"]["team_turn"] = {"target": "another_extractor"}
    assert configure_document_pipelines(result)["extractor"]["team_turn"] == {
        "target": "another_extractor",
    }


def test_http_sample_matches_sdk_document_profiles():
    path = Path(__file__).resolve().parents[3] / "deploy/local/document-memory-http.yaml"
    expected = yaml.safe_load(path.read_text(encoding="utf-8"))["memory_api"]
    baseline = deepcopy(expected)
    baseline.pop("pipeline")
    for section, names in {
        "extractor": ("document_fact", "team_turn"),
        "evolver": ("document_fact", "team_turn"),
        "classifier": ("team_conclusion",),
        "dedup": ("project_team",),
    }.items():
        for name in names:
            baseline[section].pop(name)
    assert configure_document_pipelines(baseline) == expected
