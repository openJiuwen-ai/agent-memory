"""Data-destination declarations are fail-closed (F17 D9)."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.egress import EgressInventory, declaration_for, declare, inventory
from jiuwen_memory.common.embedder.embedder_impl import openai_embedder  # noqa: F401
from jiuwen_memory.common.llm.llm_impl import echo_llm, openai_llm  # noqa: F401
from jiuwen_memory.common.reranker.reranker_impl import api_reranker  # noqa: F401

pytestmark = pytest.mark.unit


def test_declared_remote_component_lists_destination_and_kinds():
    rows = inventory(
        [("reranker.default", "reranker", "api")],
        values={"reranker_base_url": "https://rerank.example"},
    )
    row = rows[0]
    assert row.destination == "https://rerank.example"
    assert set(row.sends) == {"query", "document"}


def test_local_component_reports_no_outbound_content():
    rows = inventory([("llm.default", "llm", "echo")])
    assert rows[0].destination == "local"
    assert rows[0].sends == []


def test_undeclared_component_is_unknown_not_local():
    rows = inventory([("llm.default", "llm", "third_party_llm")])
    assert rows[0].destination == "unknown"
    assert rows[0].sends == [], "an undeclared component must not claim what it sends"


def test_default_local_template_lists_the_remote_embedder():
    inventory_rows = EgressInventory.from_settings(
        {
            "globals": {
                "embedder_base_url": "https://api.siliconflow.cn/v1",
                "llm_base_url": "https://api.deepseek.com",
            },
            "embedder": {"default": {"target": "openai", "params": {}}},
            "llm": {"default": "openai"},
        }
    ).as_list()
    embedder_row = next(row for row in inventory_rows if row["component"] == "embedder.default")
    assert embedder_row["destination"] == "https://api.siliconflow.cn/v1"
    assert embedder_row["sends"] == ["document", "query"]


def test_declaration_rejects_incoherent_kinds():
    with pytest.raises(ValueError):
        declare("x", "bad", remote=False, sends={"query"})
    with pytest.raises(ValueError):
        declare("x", "bad2", remote=True)
    assert declaration_for("x", "bad2") is None


def test_declared_namespace_is_included_and_instance_endpoint_wins():
    declare("test_outbound", "remote", remote=True, sends={"query"},
            endpoint_param="outbound_url")
    rows = EgressInventory.from_settings(
        {
            "globals": {"outbound_url": "https://global.example"},
            "test_outbound": {
                "default": {"target": "remote",
                            "params": {"outbound_url": "https://instance.example"}}
            },
        }
    ).as_list()
    row = next(item for item in rows if item["component"] == "test_outbound.default")
    assert row["destination"] == "https://instance.example"


def test_unresolved_endpoint_is_unknown_not_a_remote_guess():
    rows = inventory(
        [("llm.default", "llm", "openai")], values={}
    )
    assert rows[0].destination == "unknown"


def test_configured_category_without_declarations_is_unknown():
    rows = EgressInventory.from_settings(
        {"extractor": {"default": {"target": "third_party_extractor"}}}
    ).as_list()
    row = next(item for item in rows if item["component"] == "extractor.default")
    assert row["destination"] == "unknown"


def test_builtin_build_component_is_local_but_a_third_party_one_is_not():
    # The declaration is per target: the shipped implementation is in-process, a
    # third-party plugin in the same category must still surface as unknown.
    rows = EgressInventory.from_settings(
        {
            "chunker": {"default": {"target": "fixed_window"}},
            "extractor": {"builtin": {"target": "agent_llm"},
                          "custom": {"target": "third_party_extractor"}},
            "lifecycle": {"default": "kv"},
        }
    ).as_list()
    destinations = {row["component"]: row["destination"] for row in rows}
    assert destinations["chunker.default"] == "local"
    assert destinations["extractor.builtin"] == "local"
    assert destinations["lifecycle.default"] == "local"
    assert destinations["extractor.custom"] == "unknown"
