"""建空间时归属登记的三种形态（F07「空间开通契约」）。"""

import pytest

from jiuwen_memory.api.memory_api_impl.local_memory_api import _resolve_space_owner
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import SpaceSpec


def test_owner_unset_falls_back_to_the_caller():
    """不填即「谁建的空间归谁」——少这一步，调用方给自己建的空间自己也访问不了。"""
    caller = Scope(org="acme", user="alice")
    resolved = _resolve_space_owner(SpaceSpec(org="acme", space="s1"), caller)
    assert resolved.owner == caller


def test_owner_empty_scope_stays_unregistered():
    """主体维全空即显式不登记，共享空间取此形态；不得被回落逻辑改写成调用方。"""
    resolved = _resolve_space_owner(
        SpaceSpec(org="acme", space="team", owner=Scope()), Scope(org="acme", user="alice")
    )
    assert resolved.owner == Scope()


def test_named_owner_passes_through():
    resolved = _resolve_space_owner(
        SpaceSpec(org="acme", space="u-bob", owner=Scope(user="bob")),
        Scope(org="acme", user="svc"),
    )
    assert resolved.owner == Scope(user="bob")


def test_cross_org_owner_is_rejected():
    with pytest.raises(ValidationError, match="must belong to org"):
        _resolve_space_owner(
            SpaceSpec(org="acme", space="s1", owner=Scope(org="other", user="bob")),
            Scope(org="acme", user="svc"),
        )


def test_two_dimensional_owner_is_rejected():
    """双维登记项在反查索引处也会被拒，但那时主数据已写——须在 API 层前置拦下。"""
    with pytest.raises(ValidationError, match="exactly one of user/agent"):
        _resolve_space_owner(
            SpaceSpec(org="acme", space="s1", owner=Scope(user="bob", agent="a1")),
            Scope(org="acme", user="svc"),
        )
