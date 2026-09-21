from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.principal import AUTHOR_AGENT, AUTHOR_PRINCIPAL
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control import BatchWriteItem
from tests.support.scoped_authenticator import ScopedAuthenticator
from tests.unit.api.fixtures import security_for

pytestmark = pytest.mark.unit


def _marks(unit) -> tuple[str, str]:
    metadata = unit.system_metadata or {}
    return metadata.get(AUTHOR_PRINCIPAL), metadata.get(AUTHOR_AGENT)


def test_add_writes_author_marks_from_the_caller_identity() -> None:
    api = build_kernel().api

    # 双主体仅描述资源归属；测试显式创建委托，以单主体 agent 代用户写入。
    via_agent = Scope(org="acme", user="alice", agent="a1")
    units = api.add("hello", via_agent, security=security_for(api, via_agent))
    # 代理链上有人类主体即归人类；两个键恒写入，取值为空也不省略
    assert _marks(units[0]) == ("user:alice", "a1")

    autonomous = Scope(org="acme", agent="a1")
    units = api.add("solo", autonomous, security=internal_context(ScopedAuthenticator(autonomous)))
    assert _marks(units[0]) == ("agent:a1", "")


def test_author_marks_are_reserved_against_caller_supplied_metadata() -> None:
    api = build_kernel().api
    for key in (AUTHOR_PRINCIPAL, AUTHOR_AGENT):
        with pytest.raises(ValidationError):
            scope = Scope(org="acme", user="alice")
            api.add(
                "hello",
                scope,
                security=internal_context(ScopedAuthenticator(scope)),
                system_metadata={key: "forged"},
            )


def test_batch_add_marks_each_item_without_echoing_the_marks_back() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")
    result = api.batch_add(
        [BatchWriteItem(content="first"), BatchWriteItem(content="second")],
        scope,
        security=internal_context(ScopedAuthenticator(scope)),
        user_metadata={"project": "batch"},
    )

    assert all(not outcome.error for outcome in result.outcomes)
    for outcome in result.outcomes:
        # 内核标记落在条目上……
        assert _marks(outcome.units[0]) == ("user:alice", "")
        # ……逐项结果回填的仍是调用方输入的归一化形态
        assert outcome.item.user_metadata == {"project": "batch"}


def test_root_write_to_org_scope_keeps_named_author() -> None:
    """运维身份须具名；组织级目标不再被当作认证身份。"""
    api = build_kernel().api
    target = Scope(org="acme")
    ops = Scope(org="system", user="ops")
    units = api.add(
        "hello", target, security=internal_context(ScopedAuthenticator(ops, role=Role.ROOT))
    )
    assert units
    metadata = units[0].system_metadata or {}
    assert metadata[AUTHOR_PRINCIPAL] == "user:ops"
    assert metadata[AUTHOR_AGENT] == ""


def test_root_batch_write_to_org_scope_keeps_named_author() -> None:
    """批量运维写入同样记录真正的具名 actor。"""
    api = build_kernel().api
    target = Scope(org="acme")
    ops = Scope(org="system", user="ops")
    result = api.batch_add(
        [BatchWriteItem(content="first"), BatchWriteItem(content="second")],
        target,
        security=internal_context(ScopedAuthenticator(ops, role=Role.ROOT)),
    )
    assert all(not outcome.error for outcome in result.outcomes)
    for outcome in result.outcomes:
        metadata = outcome.units[0].system_metadata or {}
        assert metadata[AUTHOR_PRINCIPAL] == "user:ops"
        assert metadata[AUTHOR_AGENT] == ""
