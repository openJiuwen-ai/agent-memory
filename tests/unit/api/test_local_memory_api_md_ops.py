"""LocalMemoryAPI.read_md_file / edit_md_file —— MdOpsMixin PEP + 委托 + 剥瞬态键（F08）。

MdOpsMixin 经 Engine 窄端口委托（非经 ``_commands.write``/``_queries.get``），鉴权对齐
``get``（READ 单段）/ ``add``（WRITE 单段，D5），target 是折算后的反查 scope（D5.1）。
用 ``assemble()`` 默认装配出真实 LocalMemoryAPI（真 PEP/审计/space），替换
``_queries``/``_commands`` 为 FakeEngine 绑定版隔离测试 PEP + 剥瞬态键逻辑。

失效方向：
- 鉴权 action 用错（read 用 WRITE / edit 用 READ）→ 权限矩阵错配；
- 未剥瞬态键（coords）→ 瞬态键越过 API 边界回显给调用方（与 add 同病）；
- ENTRY_RULES 未登记 edit_md_file → 装配空间级判定时 FROZEN 空间漏状态校验（D5）；
- 鉴权/审计留在 raw scope 而数据面按折算空间取数（D5.1 修复前形态）→ 空 space 绕过
  空间轴判权，调用方可读无权限的任意 coords.project 折算空间（越权读取），且审计
  看不出实际访问的空间（审计盲区）。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试替换内部端口对象以隔离委托
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.api.memory_api import MemoryAPI
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.api.memory_api_impl.local_memory_api import LocalMemoryAPI
from jiuwen_memory.api.memory_api_impl.local_support import (
    _md_memory_class,
    space_scope_for_md_read,
)
from jiuwen_memory.api.memory_api_impl.md_ops import MdOpsMixin
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security import internal_context, principal
from jiuwen_memory.common.security.space_roles import ENTRY_RULES, SpaceAction, SpaceAxis
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import COORDS_KEY, Context, MemoryUnit, Scope, Segment
from jiuwen_memory.config import Config
from jiuwen_memory.construction.router import parse_route_table
from jiuwen_memory.control import SpaceSpec
from jiuwen_memory.control.application import MemoryCommandService, MemoryQueryService
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


class _FakeEngine:
    """记录调用；返回带瞬态键的 unit 以验证出口剥离。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.last_read_scope: Scope | None = None

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


def test_read_md_file_denies_routed_space_without_read_permission() -> None:
    """折算出的空间级 scope 是鉴权 target：u1 对 p_p1 无读权 → PEP 拒绝（D5.1）。

    修复前（8aab9af 初版）鉴权留在 raw scope——空 space 绕过空间轴判权，调用方仅凭
    自己 raw scope 的 READ 即可读任意 coords.project 折算出的项目空间（越权读取）。
    修复后鉴权随折算同轴：对折算空间无读权在 PEP 拒绝，不委托 Engine。
    """
    api, fake = _api_with_routed_table()
    security = internal_context(ScopedAuthenticator(SCOPE))
    with pytest.raises(PermissionDeniedError):
        api.read_md_file(_context("p1"), "MEMORY.md", security=security)
    assert len(fake.calls) == 0, "越权读取必须在委托 Engine 之前被 PEP 拒绝"


def test_read_md_file_delegates_projected_scope_for_authorized_root() -> None:
    """对折算空间有读权（ROOT 全局放行）→ 通过，数据面收到空间级 scope（折算保留）。

    与上一条互补：堵越权不能堵折算链路本身——有权限者仍按折算后的 org+space 反查。
    """
    api, fake = _api_with_routed_table()
    security = internal_context(ScopedAuthenticator(SCOPE, role=Role.ROOT))
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


# -- read_md_file 空间级判定装配：越权读取 / 审计盲区修复端到端（D5.1）------------ #

_ORG = "acme"
_PROJECT = "proj"
_PROJECT_SPACE = "p_proj"
ALICE = Scope(org=_ORG, user="alice")
BOB = Scope(org=_ORG, user="bob")
_SEC_OPS_ADMIN = internal_context(
    ScopedAuthenticator(Scope(org=_ORG, user="ops"), role=Role.ADMIN)
)

_ENGINE_COMPONENT_NAMES = (
    "ingestor",
    "index_builder",
    "retriever",
    "kv_store",
    "scheduler",
    "evolver",
    "lifecycle",
)


def _space_aware_api():
    """space_aware 判定 + 判定表 + FakeEngine：聚焦 PEP 层的空间轴判权接线。

    装配形态与 ``tests/unit/api/test_space_aware_authorization.py`` 的 ``_kernel()`` 同源
    （cloud 引擎 + space_aware permission/authorizer）；数据面替换为 FakeEngine——越权
    发生在 PEP 与数据面 scope 劈开处，修复与验证都在 PEP 层，不依赖真实存储。
    """
    engine_params = {name: "default" for name in _ENGINE_COMPONENT_NAMES}
    kernel = build_kernel(
        config=Config.from_dict(
            {
                "engine": {"default": {"target": "cloud", "params": engine_params}},
                "permission": {
                    "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
                },
                "authorizer": {
                    "default": {
                        "target": "space_aware",
                        "params": {"delegate": "standard"},
                    },
                    "standard": {
                        "target": "standard",
                        "params": {"grant_store": "default", "delegation_store": "default"},
                    },
                },
                "grant_store": {"default": "memory"},
                "delegation_store": {"default": "memory"},
            }
        )
    )
    api = kernel.api
    api._router = SimpleNamespace(table=parse_route_table(_MD_ROUTE_TABLE))
    fake = _FakeEngine()
    api._queries = MemoryQueryService(fake)
    api._commands = MemoryCommandService(fake)
    return api, fake


def _audit_recorder(api, monkeypatch) -> list[tuple[str, Scope | None, str]]:
    """捕获 ``_record_audit`` 入参（action, target_scope, decision）——PEP 的唯一审计入口。

    断言审计轴（target_scope）而非落库细节：D5.1 的审计盲区修复就是 target_scope 从
    raw scope 换成折算后的空间级 scope，在入口捕获即最短路径。
    """
    recorded: list[tuple[str, Scope | None, str]] = []
    monkeypatch.setattr(
        api,
        "_record_audit",
        lambda ident, act, **kw: recorded.append(
            (act, kw.get("target_scope"), kw.get("decision", ""))
        ),
    )
    return recorded


def test_read_md_file_space_entry_denies_non_member(monkeypatch) -> None:
    """空间级判定装配：非成员带 project coords 读他人项目空间 → 空间轴拒绝（D5.1）。

    修复前该调用通过 raw scope 鉴权（空 space 绕过空间轴）后按折算空间取数——
    bob 可读 alice 的项目空间。修复后折算空间即鉴权 target，bob 非成员被拒；
    deny 审计同样记折算后的空间级 target，拒绝事件可定位越权目标空间。
    """
    api, fake = _space_aware_api()
    api.create_space(
        SpaceSpec(org=_ORG, space=_PROJECT_SPACE, owner=ALICE), security=_SEC_OPS_ADMIN
    )
    recorded = _audit_recorder(api, monkeypatch)

    with pytest.raises(PermissionDeniedError):
        api.read_md_file(
            Context(scope=BOB, extensions={"coords": {"project": _PROJECT}}),
            "MEMORY.md",
            security=internal_context(ScopedAuthenticator(BOB)),
        )

    assert len(fake.calls) == 0, "空间轴拒绝必须发生在委托 Engine 之前"
    denies = [r for r in recorded if r[2] == "deny" and r[0] == "read_md_file"]
    assert denies, f"deny 审计缺失: {recorded}"
    assert denies[-1][1] == Scope(org=_ORG, space=_PROJECT_SPACE), (
        f"deny 审计必须记折算后的空间级 target（审计盲区）: {denies[-1][1]}"
    )


def test_read_md_file_space_entry_allows_owner_and_audits_space(monkeypatch) -> None:
    """空间级判定装配：owner 读自己的项目空间 → 放行，委托与审计都在空间级 scope。

    堵越权不能堵合法读回：写入侧（add 判定落盘空间）能写进去的 owner，读取侧对同一
    折算空间判 READ 必须放行——读写两侧对称，权限矩阵上 READ 弱于 WRITE，能写即能读。
    """
    api, fake = _space_aware_api()
    api.create_space(
        SpaceSpec(org=_ORG, space=_PROJECT_SPACE, owner=ALICE), security=_SEC_OPS_ADMIN
    )
    recorded = _audit_recorder(api, monkeypatch)

    api.read_md_file(
        Context(scope=ALICE, extensions={"coords": {"project": _PROJECT}}),
        "MEMORY.md",
        security=internal_context(ScopedAuthenticator(ALICE)),
    )

    assert fake.last_read_scope == Scope(org=_ORG, space=_PROJECT_SPACE), (
        "有权限者必须按折算后的空间级 scope 反查（折算链路保留）"
    )
    allows = [r for r in recorded if r[2] == "allow" and r[0] == "read_md_file"]
    assert allows, f"allow 审计缺失: {recorded}"
    assert allows[-1][1] == Scope(org=_ORG, space=_PROJECT_SPACE), (
        f"allow 审计必须记实际访问的空间（审计盲区）: {allows[-1][1]}"
    )


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
