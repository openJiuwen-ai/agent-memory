"""范围聚合只使用已授权回调，且显式报告截断或部分失败。"""

import pytest

from jiuwen_memory.common.errors import ConflictError, UnsupportedCapabilityError, ValidationError
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Scope, Segment
from jiuwen_memory.control.application.personal_memory import list_personal, search_personal
from jiuwen_memory.control.types import MemoryListResult
from jiuwen_memory.retrieval.types import RetrievalResult, RetrievedItem

pytestmark = pytest.mark.unit


def test_list_source_filter_and_stale_page():
    a, b = Scope(org="a", user="u"), Scope(org="b", user="u")
    units = [
        MemoryUnit(id="1", scope=a, system_metadata={"source_app": "jiuwenswarm"}),
        MemoryUnit(id="2", scope=b, system_metadata={"source_app": "portal"}),
    ]

    def read(scope, offset, limit):
        matches = [unit for unit in units if unit.scope == scope]
        return MemoryListResult(matches[offset:offset + limit], len(matches))

    page = list_personal([a, b], read, limit=1)
    assert page.count == 2
    assert list_personal([a, b], read, offset=1, limit=1, snapshot=page.snapshot).items[0].id == "2"
    assert list_personal([a, b], read, source="portal").items[0].id == "2"
    units.append(MemoryUnit(id="3", scope=a))
    with pytest.raises(ConflictError):
        list_personal([a, b], read, offset=1, snapshot=page.snapshot)
    with pytest.raises(UnsupportedCapabilityError):
        list_personal([a, b], read, max_units=1)


def test_list_combines_agent_project_and_content_tag_filters_before_paging():
    scope = Scope(org="local", user="owner", agent="design")
    units = [MemoryUnit(
        id=str(i), scope=scope, segments=[Segment(content=f"记忆 {i}")], tags=["Python"],
        system_metadata={
            "source_app": "swarm", "agent_label": "设计助手", "coords": {"project": "portal"},
        },
    ) for i in range(3)]
    other = MemoryUnit(id="other", scope=scope, segments=[Segment(content="不匹配")])

    def read(scope, offset, limit):
        return MemoryListResult((units + [other])[offset:offset + limit], 4)
    page = list_personal([scope], read, agent="design", project="portal", query="python", limit=1)
    assert page.count == 3 and len(page.items) == 1
    assert page.agents == (("design", "设计助手"),)
    assert page.projects == ("", "portal")
    assert list_personal([scope], read, agent="other").count == 0
    assert list_personal([scope], read, project="").items == [other]
    following = list_personal([scope], read, agent="design", project="portal", query="python",
                              offset=1, limit=1, snapshot=page.snapshot)
    assert following.items[0].id != page.items[0].id


def test_list_project_prefers_scalar_metadata_and_falls_back_to_coords():
    scope = Scope(org="local", user="owner")
    units = [
        MemoryUnit(id="a", scope=scope, system_metadata={"project": "alpha"}),
        MemoryUnit(id="b", scope=scope, system_metadata={"coords": {"project": "beta"}}),
        MemoryUnit(
            id="c", scope=scope, system_metadata={"project": "alpha", "coords": {"project": "beta"}}
        ),
        MemoryUnit(id="d", scope=scope),
    ]

    def read(scope, offset, limit):
        return MemoryListResult(units[offset:offset + limit], len(units))

    page = list_personal([scope], read)
    assert page.projects == ("", "alpha", "beta")
    assert [unit.id for unit in list_personal([scope], read, project="alpha").items] == ["a", "c"]
    assert [unit.id for unit in list_personal([scope], read, project="beta").items] == ["b"]


def test_list_memory_class_and_all_tiers_are_independent_filters():
    scope = Scope(org="local", user="owner")
    units = [
        MemoryUnit(
            id=tier.value, scope=scope, tier=tier,
            system_metadata={"memory_class": "custom_class"},
        ) for tier in MemoryTier
    ] + [MemoryUnit(id="unclassified", scope=scope, tier=MemoryTier.CORE)]

    def read(scope, offset, limit):
        return MemoryListResult(units[offset:offset + limit], len(units))

    page = list_personal([scope], read, memory_class="custom_class", limit=1)
    assert page.count == len(MemoryTier)
    assert page.memory_classes == ("", "custom_class")
    for tier in MemoryTier:
        filtered = list_personal([scope], read, memory_class="custom_class", tier=tier.value)
        assert [unit.id for unit in filtered.items] == [tier.value]
        assert filtered.memory_classes == ("", "custom_class"), "options must not shrink to a page"
    assert list_personal([scope], read, memory_class="").items[0].id == "unclassified"
    with pytest.raises(ValidationError, match="tier"):
        list_personal([scope], read, tier="not-a-tier")


def test_search_distinguishes_equal_ids_and_checks_authorized_location():
    a, b = Scope(org="a"), Scope(org="b")
    units = {
        "a": MemoryUnit(id="x", scope=a, segments=[Segment(content="one")]),
        "b": MemoryUnit(id="x", scope=b, segments=[Segment(content="two")]),
    }

    def get(scope, unit_id):
        unit = units.get(scope.org)
        assert unit is not None, scope
        return unit

    def search(scope, top_k):
        unit = get(scope, "x")
        score = 0.3 if scope.org == "a" else 0.9
        return RetrievalResult(
            items=[RetrievedItem(unit_id=unit.id, score=score, content=unit.content)]
        )

    hits = search_personal([a, b], search, get, top_k=2)
    assert [hit.scope for hit in hits] == [a, b]
    assert [hit.item.content for hit in hits] == ["one", "two"]
    with pytest.raises(ValidationError):
        search_personal([a], search, lambda *_: units["b"], top_k=1)


def test_cross_scope_search_round_robins_by_rank_without_global_sort():
    a, b = Scope(org="a"), Scope(org="b")
    units = {
        ("a", "a1"): MemoryUnit(id="a1", scope=a, segments=[Segment(content="a1")]),
        ("a", "a2"): MemoryUnit(id="a2", scope=a, segments=[Segment(content="a2")]),
        ("b", "b1"): MemoryUnit(id="b1", scope=b, segments=[Segment(content="b1")]),
        ("b", "b2"): MemoryUnit(id="b2", scope=b, segments=[Segment(content="b2")]),
    }

    def search(scope, top_k):
        scoped = [unit for unit in units.values() if unit.scope == scope][:top_k]
        score = 0.1 if scope.org == "a" else 0.9
        return RetrievalResult(items=[
            RetrievedItem(unit_id=unit.id, score=score, content=unit.content) for unit in scoped
        ])

    hits = search_personal(
        [a, b], search, lambda scope, unit_id: units[(scope.org, unit_id)],
        top_k=3,
    )
    assert [(hit.scope.org, hit.item.unit_id) for hit in hits] == [
        ("a", "a1"), ("b", "b1"), ("a", "a2"),
    ]


def test_search_returns_authorized_current_snapshot_not_old_disclosure():
    scope = Scope(org="a")
    unit = MemoryUnit(id="x", scope=scope, segments=[Segment(content="new content")])
    stale = RetrievedItem(
        unit_id="x", score=0.8, content="[full]\nold content", abstract="old secret"
    )
    hits = search_personal(
        [scope], lambda *_: RetrievalResult(items=[stale]), lambda *_: unit,
        top_k=1,
    )
    assert hits[0].item.content == "new content"
    assert "old secret" not in hits[0].item.abstract


def test_different_source_metadata_is_shown_as_unlabeled_after_inheritance():
    from jiuwen_memory.common.type_def.memory import inherited_system_metadata

    scope = Scope(org="a")
    first = MemoryUnit(scope=scope, system_metadata={"source_app": "first"})
    second = MemoryUnit(scope=scope, system_metadata={"source_app": "second"})
    merged = MemoryUnit(
        id="merged", scope=scope, system_metadata=inherited_system_metadata([first, second])
    )
    assert "source_app" not in merged.system_metadata
    page = list_personal(
        [scope], lambda scope, offset, limit: MemoryListResult([merged], 1), source=""
    )
    assert page.items == [merged]
    assert page.sources == ("",)
