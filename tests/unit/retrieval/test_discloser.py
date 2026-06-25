"""Discloser tests: pure content shaping over pre-loaded units (Option B)."""

from __future__ import annotations

import pytest

from config import AssemblyContext
from retrieval.discloser_impl import DiscloserProducer
from retrieval.discloser_impl.structured_discloser import StructuredDiscloser
from retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from retrieval.types import (
    ChannelEvidence,
    DisclosureLevel,
    ParsedQuery,
    RecallChannel,
    ScoredUnit,
)

pytestmark = pytest.mark.unit


def test_l0_truncates_and_l2_returns_full_content(unit_factory) -> None:
    content = "word " * 40
    unit = unit_factory("u1", content)
    discloser = TruncatingDiscloser()
    candidates = [ScoredUnit("u1", 1.0, RecallChannel.VECTOR)]
    units = {"u1": unit}

    l2 = discloser.disclose(ParsedQuery(raw="x"), candidates, units, DisclosureLevel.L2)[0]
    l0 = discloser.disclose(ParsedQuery(raw="x"), candidates, units, DisclosureLevel.L0)[0]

    assert l2.content == content
    assert len(l0.content) < len(l2.content)
    assert l0.content.endswith("…")


def test_l1_returns_window_around_keyword(unit_factory) -> None:
    content = "intro padding " * 5 + "the coffee bean is roasted " + "tail " * 20
    unit = unit_factory("u1", content)
    discloser = TruncatingDiscloser()
    candidates = [ScoredUnit("u1", 1.0, RecallChannel.KEYWORD)]

    item = discloser.disclose(
        ParsedQuery(raw="coffee", keywords=["coffee"]), candidates, {"u1": unit}, DisclosureLevel.L1
    )[0]

    assert "coffee" in item.content
    assert len(item.content) < len(content)


def test_skips_candidate_without_loaded_unit() -> None:
    discloser = TruncatingDiscloser()
    candidates = [ScoredUnit("missing", 1.0, RecallChannel.VECTOR)]

    results = discloser.disclose(ParsedQuery(raw="x"), candidates, {}, DisclosureLevel.L2)

    assert results == []


def test_truncating_adaptive_falls_back_to_l0(unit_factory) -> None:
    unit = unit_factory("u1", "word " * 40)
    discloser = TruncatingDiscloser()
    candidates = [ScoredUnit("u1", 1.0, RecallChannel.VECTOR)]

    item = discloser.disclose(
        ParsedQuery(raw="word"),
        candidates,
        {"u1": unit},
        DisclosureLevel.ADAPTIVE,
        max_tokens=1000,
    )[0]

    assert item.level == DisclosureLevel.L0
    assert item.content.endswith("…")


def test_structured_l0_returns_memory_card(unit_factory) -> None:
    unit = unit_factory("u1", "packages/foo should use pnpm for dependency installs.", tags=["repo"])
    unit.metadata["summary"] = "packages/foo 使用 pnpm 作为包管理器。"
    discloser = StructuredDiscloser()
    candidates = [
        ScoredUnit(
            "u1",
            0.2,
            RecallChannel.KEYWORD,
            evidence=[
                ChannelEvidence(
                    channel=RecallChannel.KEYWORD,
                    rank=0,
                    score=0.9,
                    weight=2.0,
                    contribution=0.2,
                )
            ],
        )
    ]

    item = discloser.disclose(
        ParsedQuery(raw="package manager"),
        candidates,
        {"u1": unit},
        DisclosureLevel.L0,
    )[0]

    assert "[summary] packages/foo 使用 pnpm 作为包管理器。" in item.content
    assert "[why] keyword(rank=1,score=0.9,weight=2,contribution=0.2)" in item.content
    assert "[scope] org=acme user=u1 agent=a1 session=s1" in item.content
    assert "[tags] repo" in item.content
    assert "[lifecycle] active" in item.content


def test_structured_l1_returns_query_focused_evidence(unit_factory) -> None:
    content = (
        "root workspace uses npm. "
        + "padding " * 40
        + "packages/foo package manager is pnpm and dependency installs must run there. "
        + "tail " * 20
    )
    unit = unit_factory("u1", content)
    discloser = StructuredDiscloser()
    candidates = [ScoredUnit("u1", 1.0, RecallChannel.KEYWORD)]

    item = discloser.disclose(
        ParsedQuery(raw="pnpm package", keywords=["pnpm", "package"]),
        candidates,
        {"u1": unit},
        DisclosureLevel.L1,
    )[0]

    assert item.level == DisclosureLevel.L1
    assert "[evidence]" in item.content
    assert "packages/foo package manager is pnpm" in item.content
    assert "[matched] pnpm, package" in item.content
    evidence = [line for line in item.content.splitlines() if line.startswith("[evidence]")][0]
    assert "root workspace uses npm" not in evidence


def test_structured_l1_falls_back_to_l0_without_keyword_hit(unit_factory) -> None:
    unit = unit_factory("u1", "alice likes coffee in the morning")
    discloser = StructuredDiscloser()
    candidates = [ScoredUnit("u1", 1.0, RecallChannel.KEYWORD)]

    item = discloser.disclose(
        ParsedQuery(raw="tea", keywords=["tea"]),
        candidates,
        {"u1": unit},
        DisclosureLevel.L1,
    )[0]

    assert item.level == DisclosureLevel.L1
    assert "[summary] alice likes coffee in the morning" in item.content
    assert "[evidence]" not in item.content


def test_structured_l2_returns_full_content_with_header(unit_factory) -> None:
    content = "full memory content"
    unit = unit_factory("u1", content)
    discloser = StructuredDiscloser()
    candidates = [ScoredUnit("u1", 1.0, RecallChannel.VECTOR)]

    item = discloser.disclose(
        ParsedQuery(raw="memory"),
        candidates,
        {"u1": unit},
        DisclosureLevel.L2,
    )[0]

    assert item.content == "[full]\n" + content


def test_structured_adaptive_uses_l0_when_budget_is_tight(unit_factory) -> None:
    unit1 = unit_factory("u1", "packages/foo package manager is pnpm. " + "detail " * 20)
    unit2 = unit_factory("u2", "root workspace package manager is npm. " + "detail " * 20)
    discloser = StructuredDiscloser()
    candidates = [
        ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
        ScoredUnit("u2", 0.9, RecallChannel.KEYWORD),
    ]

    items = discloser.disclose(
        ParsedQuery(raw="package manager", keywords=["package", "manager"]),
        candidates,
        {"u1": unit1, "u2": unit2},
        DisclosureLevel.ADAPTIVE,
        max_tokens=1,
    )

    assert [item.level for item in items] == [DisclosureLevel.L0, DisclosureLevel.L0]


def test_structured_adaptive_upgrades_items_to_l1_with_budget(unit_factory) -> None:
    unit1 = unit_factory("u1", "packages/foo package manager is pnpm. " + "detail " * 5)
    unit2 = unit_factory("u2", "root workspace package manager is npm. " + "detail " * 5)
    discloser = StructuredDiscloser()
    candidates = [
        ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
        ScoredUnit("u2", 1.0, RecallChannel.KEYWORD),
    ]

    items = discloser.disclose(
        ParsedQuery(raw="package manager", keywords=["package", "manager"]),
        candidates,
        {"u1": unit1, "u2": unit2},
        DisclosureLevel.ADAPTIVE,
        max_tokens=1000,
    )

    assert [item.level for item in items] == [DisclosureLevel.L1, DisclosureLevel.L1]
    assert all("[evidence]" in item.content for item in items)


def test_structured_adaptive_upgrades_top_hit_to_l2_when_confident(unit_factory) -> None:
    unit1 = unit_factory("u1", "packages/foo package manager is pnpm. " + "detail " * 20)
    unit2 = unit_factory("u2", "root workspace package manager is npm. " + "detail " * 20)
    discloser = StructuredDiscloser()
    candidates = [
        ScoredUnit("u1", 3.0, RecallChannel.KEYWORD),
        ScoredUnit("u2", 1.0, RecallChannel.KEYWORD),
    ]

    items = discloser.disclose(
        ParsedQuery(raw="package manager", keywords=["package", "manager"]),
        candidates,
        {"u1": unit1, "u2": unit2},
        DisclosureLevel.ADAPTIVE,
        max_tokens=1000,
    )

    assert [item.level for item in items] == [DisclosureLevel.L2, DisclosureLevel.L1]
    assert items[0].content.startswith("[full]\n")


def test_structured_discloser_can_be_created_from_config() -> None:
    discloser = DiscloserProducer.build("structured", {}, AssemblyContext())

    assert isinstance(discloser, StructuredDiscloser)
