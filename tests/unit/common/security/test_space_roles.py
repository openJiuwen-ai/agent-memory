from __future__ import annotations

import pytest

from jiuwen_memory.common.security.space_roles import (
    CONTENT_RANK,
    GOVERNANCE_RANK,
    OWNER_ENTRY_COVERS,
    OWNER_ENTRY_SAME_DIMS,
    SpaceAuthorizationFacts,
    SpaceContentRole,
    SpaceGovernanceRole,
    SpaceMemberFact,
    most_specific,
)
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit


def _fact(
    *,
    user: str = "",
    agent: str = "",
    content: SpaceContentRole = SpaceContentRole.VIEWER,
    governance: SpaceGovernanceRole = SpaceGovernanceRole.NONE,
) -> SpaceMemberFact:
    return SpaceMemberFact(
        scope=Scope(org="acme", space="team", user=user, agent=agent),
        content_role=content,
        governance_role=governance,
    )


def test_is_individual_tracks_membership_emptiness() -> None:
    assert SpaceAuthorizationFacts().is_individual is True
    assert SpaceAuthorizationFacts(members=(_fact(user="alice"),)).is_individual is False


def test_ranks_are_ordered_within_each_axis() -> None:
    assert CONTENT_RANK[SpaceContentRole.EDITOR] > CONTENT_RANK[SpaceContentRole.CONTRIBUTOR]
    assert CONTENT_RANK[SpaceContentRole.CONTRIBUTOR] > CONTENT_RANK[SpaceContentRole.VIEWER]
    assert GOVERNANCE_RANK[SpaceGovernanceRole.OWNER] > GOVERNANCE_RANK[SpaceGovernanceRole.MANAGER]


def test_owner_entry_levels_do_not_overlap() -> None:
    """两级不重叠：逐维相同者自然满足覆盖，第二级只列它独有的入口。"""
    assert not (OWNER_ENTRY_SAME_DIMS & OWNER_ENTRY_COVERS)
    assert "export_space" in OWNER_ENTRY_SAME_DIMS  # 整空间导出只许本人直接调用
    assert "get_space" in OWNER_ENTRY_COVERS


def test_most_specific_wildcard_only_matches_the_user_dimension() -> None:
    """组织通配记录不进 agent 维候选。

    少这一条，具名代理的档位提升会被通配记录压回，组织内全体共享与代理跨用户复用
    两类场景都依赖它。
    """
    wildcard = _fact(content=SpaceContentRole.VIEWER)
    named_agent = _fact(agent="a1", content=SpaceContentRole.CONTRIBUTOR)
    members = (wildcard, named_agent)
    actor = Scope(org="acme", user="alice", agent="a1")

    assert most_specific(members, actor, dim="user") is wildcard
    assert most_specific(members, actor, dim="agent") is named_agent


def test_most_specific_prefers_the_two_dimension_record_on_the_agent_axis() -> None:
    single_agent = _fact(agent="a1", content=SpaceContentRole.VIEWER)
    both_dims = _fact(user="alice", agent="a1", content=SpaceContentRole.EDITOR)
    members = (single_agent, both_dims)

    actor = Scope(org="acme", user="alice", agent="a1")
    assert most_specific(members, actor, dim="agent") is both_dims
    # 双维记录 agent 维为非空，不进 user 维候选
    assert most_specific(members, actor, dim="user") is None


def test_most_specific_skips_two_dimension_records_of_another_user() -> None:
    foreign = _fact(user="bob", agent="a1", content=SpaceContentRole.EDITOR)
    actor = Scope(org="acme", user="alice", agent="a1")
    assert most_specific((foreign,), actor, dim="agent") is None


def test_most_specific_returns_none_without_candidates() -> None:
    actor = Scope(org="acme", user="alice")
    assert most_specific((), actor, dim="user") is None
    assert most_specific((_fact(user="bob"),), actor, dim="user") is None
