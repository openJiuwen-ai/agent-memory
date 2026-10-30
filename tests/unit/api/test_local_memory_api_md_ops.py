"""LocalMemoryAPI.read_md_file / edit_md_file —— MdOpsMixin PEP + 委托 + 剥瞬态键（F08）。

MdOpsMixin 经 Engine 窄端口委托（非经 ``_commands.write``/``_queries.get``），鉴权对齐
``get``（READ 单段）/ ``add``（WRITE 单段，D5）。用 ``assemble()`` 默认装配出真实
LocalMemoryAPI（allow_all + 真 PEP/审计/space），替换 ``_queries``/``_commands`` 为
FakeEngine 绑定版隔离测试 PEP + 剥瞬态键逻辑。

失效方向：
- 鉴权 action 用错（read 用 WRITE / edit 用 READ）→ 权限矩阵错配；
- 未剥瞬态键（coords）→ 瞬态键越过 API 边界回显给调用方（与 add 同病）；
- ENTRY_RULES 未登记 edit_md_file → 装配空间级判定时 FROZEN 空间漏状态校验（D5）。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试替换内部端口对象以隔离委托
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.api.memory_api import MemoryAPI
from jiuwen_memory.api.memory_api_impl.local_memory_api import LocalMemoryAPI
from jiuwen_memory.api.memory_api_impl.local_support import (
    _md_memory_class,
    space_scope_for_md_read,
)
from jiuwen_memory.api.memory_api_impl.md_ops import MdOpsMixin
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security import internal_context, principal
from jiuwen_memory.common.security.space_roles import ENTRY_RULES, SpaceAction, SpaceAxis
from jiuwen_memory.common.type_def import COORDS_KEY, Context, MemoryUnit, Scope, Segment
from jiuwen_memory.construction.router import parse_route_table
from jiuwen_memory.control.application import MemoryCommandService, MemoryQueryService
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


class _FakeEngine:
    """记录调用；返回带瞬态键的 unit 以验证出口剥离。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def read_md_file(self, context, md_filename, *, line_range=None):
        self.last_read_scope = context.scope
        self.calls.append(("read", md_filename, line_range))
        return [
            MemoryUnit(
                id="u1",
                scope=context.scope,
                segments=[Segment(content="hello")],
                system_metadata={"project": "p1", COORDS_KEY: {"project": "p1"}},
            )
        ]

    async def edit_md_file(self, context, md_filename, old_text, new_text):
        self.calls.append(("edit", md_filename, old_text, new_text))
        return [
            MemoryUnit(
                id="u2",
                scope=context.scope,
                segments=[Segment(content="hello NEW")],
                supersedes="u1",
                system_metadata={"project": "p1", COORDS_KEY: {"project": "p1"}},
            )
        ]


def _api_with_fake_engine():
    """assemble() 默认装配 + 替换 _queries/_commands 为 FakeEngine 绑定版。"""
    api = assemble()
    fake = _FakeEngine()
    api._queries = MemoryQueryService(fake)
    api._commands = MemoryCommandService(fake)
    return api, fake


_MD_ROUTE_TABLE = {
    "coord_entities": ["project"],
    "memory_classes": [
        {"name": "user_profile", "owner": "user", "space_template": "u_{user}", "fallback": True},
        {"name": "long_term_memory", "owner": "project", "space_template": "p_{project}"},
        {"name": "daily_memory", "owner": "team", "record_only": True},
    ],
    "narrow_dims": [{"entity": "project", "tag_key": "project_id"}],
}


def _api_with_routed_table():
    """assemble() + 注入非空判定表（routing_enabled=True）+ FakeEngine。

    不完整装配 keyword_stub Router——``_md_read_scope`` 只读 ``_route_table.naming`` 与
    ``_routing_enabled``，注入一个带 ``table`` 的替身即可聚焦验证折算接线，鉴权仍走
    assemble() 的真实角色鉴权（u1 读自己的 scope 通过）。
    """
    api = assemble()
    api._router = SimpleNamespace(table=parse_route_table(_MD_ROUTE_TABLE))
    fake = _FakeEngine()
    api._queries = MemoryQueryService(fake)
    api._commands = MemoryCommandService(fake)
    return api, fake


def _context(project: str = "p1") -> Context:
    return Context(scope=SCOPE, extensions={"coords": {"project": project}} if project else {})


# -- 静态契约 ----------------------------------------------------------------- #


def test_entry_rules_registers_md_file_ops() -> None:
    """ENTRY_RULES 登记 read_md_file/edit_md_file（D5 鉴权对齐 add/get 的必要条件）。

    不登记则装配空间级判定时 edit_md_file 在 FROZEN 空间漏掉 _ensure_space_state_allows
    （_ensure_space_writable 在该形态 early return）。
    """
    assert ENTRY_RULES["read_md_file"].axis is SpaceAxis.CONTENT
    assert ENTRY_RULES["read_md_file"].action is SpaceAction.READ
    assert ENTRY_RULES["edit_md_file"].axis is SpaceAxis.CONTENT
    assert ENTRY_RULES["edit_md_file"].action is SpaceAction.WRITE


def test_memory_api_abc_defines_md_file_methods() -> None:
    """MemoryAPI ABC 定义 read_md_file / edit_md_file 抽象方法。"""
    assert "read_md_file" in MemoryAPI.__abstractmethods__
    assert "edit_md_file" in MemoryAPI.__abstractmethods__


def test_md_ops_mixin_in_local_memory_api_mro() -> None:
    """MdOpsMixin 在 LocalMemoryAPI MRO 中（D7）。"""
    assert issubclass(LocalMemoryAPI, MdOpsMixin)


def test_no_duplicate_method_names_in_class_body() -> None:
    """同一类体内无同名方法重复定义（AGENTS.md 本地约束 #9，后定义静默覆盖先定义）。

    read_md_file / edit_md_file 是新增方法，须确认它们在各 mixin / LocalMemoryAPI 类体里
    各只定义一次，不被后续 mixin 的同名方法覆盖。
    """
    for cls in LocalMemoryAPI.__mro__:
        names = list(vars(cls))
        seen: dict[str, int] = {}
        for n in names:
            seen[n] = seen.get(n, 0) + 1
        dupes = [n for n, c in seen.items() if c > 1 and not n.startswith("__")]
        assert not dupes, f"{cls.__name__} has duplicate public method names: {dupes}"


# -- read_md_file：PEP + 委托 + 剥瞬态键 ------------------------------------- #


def test_read_md_file_authorizes_read_and_delegates() -> None:
    """PEP READ 通过 → 委托 _queries.read_md_file → 返回 MemoryUnit list。"""
    api, fake = _api_with_fake_engine()
    security = internal_context(ScopedAuthenticator(SCOPE))
    units = api.read_md_file(_context(), "MEMORY.md", security=security, line_range=(1, 5))

    assert len(units) == 1
    assert units[0].id == "u1"
    assert units[0].segments[0].content == "hello"
    assert fake.calls[-1] == ("read", "MEMORY.md", (1, 5))


def test_read_md_file_strips_transient_metadata() -> None:
    """返回前剥瞬态键：coords 剥除（路径解析依据，不是条目属性），非瞬态键保留。"""
    api, _ = _api_with_fake_engine()
    security = internal_context(ScopedAuthenticator(SCOPE))
    units = api.read_md_file(_context(), "MEMORY.md", security=security)

    meta = units[0].system_metadata or {}
    assert COORDS_KEY not in meta, f"transient coords not stripped: {meta}"
    assert "project" in meta, f"non-transient project wrongly stripped: {meta}"


def test_read_md_file_default_line_range_none() -> None:
    """line_range 默认 None 透传。"""
    api, fake = _api_with_fake_engine()
    security = internal_context(ScopedAuthenticator(SCOPE))
    api.read_md_file(_context(), "USER.md", security=security)
    assert fake.calls[-1] == ("read", "USER.md", None)


# -- read_md_file 路由折算（方案甲）：读回侧对称空间折算 -------------------- #


def test_md_memory_class_maps_filenames() -> None:
    """md 文件名 → memory_class：USER.md/MEMORY.md/daily 三态，basename 归一。"""
    assert _md_memory_class("MEMORY.md") == "long_term_memory"
    assert _md_memory_class("USER.md") == "user_profile"
    assert _md_memory_class("2026-10-29.md") == "daily_memory"
    assert _md_memory_class("memory/p1/MEMORY.md") == "long_term_memory"


def test_space_scope_for_md_read_projects_to_org_and_space() -> None:
    """MEMORY.md（long_term_memory）折算成 Scope(org, space=p_{project})，主体维空。"""
    naming = parse_route_table(_MD_ROUTE_TABLE).naming
    raw = Scope(org="acme", user="alice")
    coords = principal.kernel_coords({"project": "p1"}, raw)
    assert space_scope_for_md_read(naming, raw, "MEMORY.md", coords) == Scope(
        org="acme", space="p_p1"
    )


def test_space_scope_for_md_read_projects_user_template_via_kernel_coords() -> None:
    """USER.md（user_profile）用内核坐标 user 渲染——业务 coords 无 user，须经 kernel_coords。"""
    naming = parse_route_table(_MD_ROUTE_TABLE).naming
    raw = Scope(org="acme", user="alice")
    coords = principal.kernel_coords({"project": "p1"}, raw)
    assert space_scope_for_md_read(naming, raw, "USER.md", coords) == Scope(
        org="acme", space="u_alice"
    )


def test_space_scope_for_md_read_falls_back_when_unrenderable() -> None:
    """daily_memory 无模板 → 渲染空串 → 退回 raw scope（放宽方向）。"""
    naming = parse_route_table(_MD_ROUTE_TABLE).naming
    raw = Scope(org="acme", user="alice")
    coords = principal.kernel_coords({"project": "p1"}, raw)
    assert space_scope_for_md_read(naming, raw, "2026-10-29.md", coords) == raw


def test_read_md_file_projects_scope_to_space_level_when_routed() -> None:
    """路由启用时 read_md_file 下传空间级 scope（org+space），非 raw（org+user）。

    修复前反查用 raw scope，space/user 两段与落盘（org+space）错位 → 静默返空。
    """
    api, fake = _api_with_routed_table()
    security = internal_context(ScopedAuthenticator(SCOPE))
    api.read_md_file(_context("p1"), "MEMORY.md", security=security)

    assert fake.last_read_scope == Scope(org="acme", space="p_p1")


def test_read_md_file_keeps_raw_scope_without_coords() -> None:
    """坐标缺失时退回 raw scope——放宽方向，不阻断读取（路由启用但无反查锚点）。"""
    api, fake = _api_with_routed_table()
    security = internal_context(ScopedAuthenticator(SCOPE))
    api.read_md_file(Context(scope=SCOPE, extensions={}), "MEMORY.md", security=security)

    assert fake.last_read_scope == SCOPE


def test_read_md_file_keeps_raw_scope_when_not_routed() -> None:
    """未启用路由时 scope 原样下传——与改造前行为一致。"""
    api, fake = _api_with_fake_engine()
    security = internal_context(ScopedAuthenticator(SCOPE))
    api.read_md_file(_context("p1"), "MEMORY.md", security=security)

    assert fake.last_read_scope == SCOPE


# -- edit_md_file：PEP + 委托 + 剥瞬态键 ------------------------------------- #


def test_edit_md_file_authorizes_write_and_delegates() -> None:
    """PEP WRITE 通过 + _ensure_space_writable → 委托 _commands.edit_md_file → 返回新 unit。"""
    api, fake = _api_with_fake_engine()
    security = internal_context(ScopedAuthenticator(SCOPE))
    units = api.edit_md_file(
        _context(), "MEMORY.md", "hello", "hello NEW", security=security
    )

    assert len(units) == 1
    assert units[0].id == "u2"
    assert units[0].supersedes == "u1"
    assert fake.calls[-1] == ("edit", "MEMORY.md", "hello", "hello NEW")


def test_edit_md_file_strips_transient_metadata() -> None:
    """edit 返回前同样剥瞬态键（与 add 出口同处置）。"""
    api, _ = _api_with_fake_engine()
    security = internal_context(ScopedAuthenticator(SCOPE))
    units = api.edit_md_file(_context(), "MEMORY.md", "hello", "x", security=security)

    meta = units[0].system_metadata or {}
    assert COORDS_KEY not in meta, f"transient coords not stripped on edit: {meta}"


def test_read_md_file_denies_without_authorization() -> None:
    """鉴权是必经环节：跨主体 READ 被 PEP 拒绝，且拒绝时不委托 Engine。

    mem2.0 起 assemble() 装配的是真实角色鉴权（非旧版 allow-all）：一个 user 读不到
    另一个 user 的 scope。用不同 user（不同 org）验证跨主体读取被拒，且拒绝发生在
    委托之前（fake.calls 为空说明 PEP 先于委托拦截）。
    """
    api, fake = _api_with_fake_engine()
    security = internal_context(ScopedAuthenticator(Scope(org="other-org", user="u2")))
    with pytest.raises(PermissionDeniedError):
        api.read_md_file(
            Context(scope=SCOPE, extensions={}), "MEMORY.md", security=security
        )
    assert len(fake.calls) == 0, "PEP 拒绝时不得委托 Engine"
