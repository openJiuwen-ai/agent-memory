"""存量回填：把升级前的数据补成判定链的输入（F07「存量前提」）。

判定链上线后有五项数据成为判定或检索的输入，缺任何一项都会使存量数据在新规则下不可
访问，其中三项的失效方向是放行——「部分能用」，不会有任何调用报错：

====  ==============================================================  ========
项    动作                                                            失效方向
====  ==============================================================  ========
1     归属登记：按条目 scope 的主体维推导 ``SpaceInfo.owners``        拒绝
2     条目标记：写 ``author_principal`` / ``author_agent``            拒绝
3     scope 迁移：清空条目 scope 的主体维与会话维                     放行
4     成员表合并：逐成员键并成单键                                    放行
5     判定标签键补齐：缺失的键一律写空串                              放行
====  ==============================================================  ========

**第 5 项只能补空串，不能追溯判定。** 存量条目写入时没有归属坐标，「这条属于哪个项目」
这个信息不存在于任何地方。空串的语义是「不特定于任何坐标，因此对任何坐标都可见」——与
新写入路径上判为否的条目同一待遇，且不会被按标签的等值删除误伤（空串不等于任何具体
取值）。不补的后果是升级那一刻起全部历史记忆在带 ``coords`` 的检索里凭空消失：集合谓词
``IN ("", value)`` 在字段缺失时判为不匹配，条目查不到且不报错。

未装配 ``router`` 命名空间时第 5 项整体跳过（判定标签键集合为空）。

因此第 3 项不是可用性优化，与第 1、2 项同为判定正确性的前提：条目真源 scope 仍带
主体维时，条目级鉴权的目标取该 scope，逐维相等即放行，作者对自己写的条目取得全部
动作、不经内容轴矩阵——只读档成员因此可改可删自己写的条目。

**第 3 项是物理键搬迁，不是字段更新**：条目 scope 同时是存储的命名空间键，KV 键、
检索索引主键与图命名空间都由它拼出，主体维一变这些键全部改变。因此迁移逐条「写新 →
校验 → 删旧」，且删旧作为最后一遍独立扫描执行，而非逐条紧随写新——窗口内中止即停在
新旧并存态，回滚动作是删除已写的新键，不丢数据。

用法::

    python -m deploy.migration.backfill backfill --org acme [--space coding] [--dry-run]
    python -m deploy.migration.backfill provision-main-space --org acme --principals p.txt
    python -m deploy.migration.backfill rebuild-registry --org acme
    python -m deploy.migration.backfill rebuild-index --org acme
    python -m deploy.migration.backfill audit-shape --org acme

全部子命令接受 ``--config <yaml>`` 指定与服务同一份装配配置；不传即用内置默认
（进程内栈，仅供演练）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

from jiuwen_memory.api.memory_api_impl.assembly import _Kernel, _build_kernel
from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security import principal
from jiuwen_memory.common.type_def import (
    MEMORY_KEY_PREFIX,
    MESSAGES_KEY_PREFIX,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.config.config import Config
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.control.space_impl.kv_space_manager import (
    _INFO_KEY,
    _MEMBER_PREFIX,
    _MEMBERS_KEY,
    _ROOT_SCOPE,
    _index_key,
    _info_from_bytes,
    _info_to_bytes,
    _member_from_bytes,
    _members_from_bytes,
    _members_to_bytes,
    _principal_bucket,
    _registry_key,
    _scope,
)
from jiuwen_memory.control.types import SpaceMember, SpaceSpec
from jiuwen_memory.storage.kv import KVStore

logger = get_logger(__name__)

# 各 Producer 命名空间下的缺省具名实例名（与 config.defaults 的默认段同名）
_ASSEMBLY_DEFAULT_NAME = "default"


@dataclass
class SpaceReport:
    """单个空间的回填结果。"""

    org: str = ""
    space: str = ""
    owners_registered: list[Scope] = field(default_factory=list)
    units_scanned: int = 0
    units_marked: int = 0  # 项 2 与项 5 合计：本遍实际改写了 metadata 的条目数
    units_migrated: int = 0
    units_old_deleted: int = 0
    messages_migrated: int = 0
    members_merged: int = 0
    index_entries: int = 0
    unresolved: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"[{self.org}/{self.space}]",
            f"  归属登记      : {[_render_scope(s) for s in self.owners_registered] or '未登记'}",
            f"  条目扫描/标记 : {self.units_scanned} / {self.units_marked}",
            f"  条目迁移/删旧 : {self.units_migrated} / {self.units_old_deleted}",
            f"  原文迁移      : {self.messages_migrated}",
            f"  成员记录合并  : {self.members_merged}",
            f"  反查索引项    : {self.index_entries}",
        ]
        if self.unresolved:
            lines.append(f"  待处理        : {'; '.join(self.unresolved)}")
        return "\n".join(lines)


def _render_scope(scope: Scope) -> str:
    parts = [f"org={scope.org}", f"space={scope.space}"]
    if scope.user:
        parts.append(f"user={scope.user}")
    if scope.agent:
        parts.append(f"agent={scope.agent}")
    return "(" + ", ".join(parts) + ")"


def _scope_key(scope: Scope) -> tuple[str, ...]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _target_scope(scope: Scope) -> Scope:
    """条目落盘 scope 只有两维：主体维与会话维交由条目上的作者标记表达。"""
    return replace(scope, user="", agent="", session="")


def _author_marks_of(scope: Scope) -> tuple[str, str]:
    """由存量条目 scope 的主体维推导作者标记；两维皆空返回空串对。"""
    if scope.user:
        return f"user:{scope.user}", scope.agent
    if scope.agent:
        return f"agent:{scope.agent}", ""
    return "", ""


class Backfiller:
    """逐空间执行五项回填。全部动作幂等，中断后可重跑。"""

    def __init__(
        self,
        kv: KVStore,
        *,
        index_builder: IndexBuilder | None = None,
        tag_keys: frozenset[str] = frozenset(),
        dry_run: bool = False,
    ) -> None:
        self._kv = kv
        self._index_builder = index_builder
        # 判定标签键集合取自装配好的判定表；为空即第 5 项整体跳过。
        self._tag_keys = tag_keys
        self._dry_run = dry_run

    # -- 空间枚举 --------------------------------------------------------- #

    def spaces_of(self, org: str) -> list[str]:
        """列出该 org 下有空间元数据的空间名。"""
        found: set[str] = set()
        for scope in self._kv.scopes():
            if scope.org != org or not scope.space:
                continue
            if self._kv.exists(_scope(scope.org, scope.space), _INFO_KEY):
                found.add(scope.space)
        return sorted(found)

    def entry_scopes(self, org: str, space: str) -> list[Scope]:
        """该空间下实际用过的全部物理 scope（含带主体维的存量键空间）。"""
        return [s for s in self._kv.scopes() if s.org == org and s.space == space]

    # -- 主流程 ----------------------------------------------------------- #

    def backfill_space(self, org: str, space: str) -> SpaceReport:
        report = SpaceReport(org=org, space=space)
        # 归属登记排在 scope 迁移之前：迁移后主体维已从物理 scope 消失，重跑时改由条目的
        # 作者标记推导（_owners_from_marks），两条路径都要保留。
        self._register_owners(org, space, report)  # 项 1
        # 项 5 与项 2 合在同一遍条目改写里：两者都只改 metadata，分两遍即多扫一次全库。
        pending_delete = self._migrate_units(org, space, report)  # 项 2 + 项 5 + 项 3 的写新
        self._delete_old(pending_delete, report)  # 项 3 的删旧：独立末遍
        self._merge_members(org, space, report)  # 项 4
        self.rebuild_space_index(org, space, report)
        return report

    # -- 项 2 + 项 3：条目标记与 scope 迁移 -------------------------------- #

    def _migrate_units(
        self, org: str, space: str, report: SpaceReport
    ) -> list[tuple[Scope, str, MemoryUnit | None]]:
        """逐条写新并校验，返回待删的旧键清单（末遍统一删除）。"""
        pending: list[tuple[Scope, str, MemoryUnit | None]] = []
        target = _scope(org, space)
        for source in self.entry_scopes(org, space):
            for key, raw in list(self._kv.scan(source, prefix=MEMORY_KEY_PREFIX)):
                report.units_scanned += 1
                unit = loads(raw)
                if unit is None:
                    report.unresolved.append(f"条目字节无法解码：{_render_scope(source)}{key}")
                    continue
                marked, changed = self._rewrite_unit(unit, source)
                if source == target:  # scope 已是两维形态：只补标记
                    if changed:
                        report.units_marked += 1
                        self._put(target, key, dumps(marked))
                    elif principal.AUTHOR_PRINCIPAL not in (unit.system_metadata or {}):
                        # 条目已在两维 scope 下却没有作者标记：作者无从推导（主体维已不在
                        # 物理键上），判定生效后该条不可达，须人工指定作者或按遗留数据处置
                        report.unresolved.append(f"条目缺作者标记且无法推导：{key}")
                    continue
                if changed:
                    report.units_marked += 1
                migrated = replace(marked, scope=target)
                self._put(target, key, dumps(migrated))
                if not self._dry_run and not self._kv.exists(target, key):
                    report.unresolved.append(f"写新后校验失败，旧键保留：{key}")
                    continue
                self._build_index([migrated])
                report.units_migrated += 1
                pending.append((source, key, unit))
            for key, raw in list(self._kv.scan(source, prefix=MESSAGES_KEY_PREFIX)):
                if source == target:
                    continue
                self._put(target, key, raw)
                if not self._dry_run and not self._kv.exists(target, key):
                    report.unresolved.append(f"原文写新后校验失败，旧键保留：{key}")
                    continue
                report.messages_migrated += 1
                pending.append((source, key, None))
        return pending

    def _rewrite_unit(self, unit: MemoryUnit, source: Scope) -> tuple[MemoryUnit, bool]:
        """项 2 与项 5：补作者标记与判定标签键，返回 ``(新条目, 是否有改动)``。

        两项各自幂等：已有的键一律不覆盖——作者标记是归属判据，判定标签可能已由新写入
        路径写过，回填覆盖任何一项都会改变条目的可见性。
        """
        metadata = dict(unit.system_metadata or {})
        changed = False

        if principal.AUTHOR_PRINCIPAL not in metadata:
            author_principal, author_agent = _author_marks_of(source)
            if author_principal:  # 主体维全空：无从推导，留给形态审查
                metadata[principal.AUTHOR_PRINCIPAL] = author_principal
                metadata[principal.AUTHOR_AGENT] = author_agent
                changed = True

        for key in sorted(self._tag_keys):
            if key not in metadata:
                metadata[key] = ""
                changed = True

        return (replace(unit, system_metadata=metadata), True) if changed else (unit, False)

    def _delete_old(
        self, pending: Sequence[tuple[Scope, str, MemoryUnit | None]], report: SpaceReport
    ) -> None:
        """删旧独立末遍执行：此前新旧并存，中止即可回滚；删旧完成后不可回滚。"""
        for source, key, unit in pending:
            if unit is not None:
                self._remove_index([unit])
            if not self._dry_run:
                self._kv.delete(source, key)
            report.units_old_deleted += 1

    # -- 项 1：归属登记 ---------------------------------------------------- #

    def _register_owners(self, org: str, space: str, report: SpaceReport) -> None:
        """按条目的作者标记推导归属主体；已登记即跳过。

        单一主体登记一项；多个主体逐一登记（多归属空间在判定侧另有三项限制）；
        无条目或主体维全空则不登记，进待处理清单。
        """
        try:
            info = _info_from_bytes(self._kv.get(_scope(org, space), _INFO_KEY))
        except NotFoundError:
            report.unresolved.append("空间元数据缺失，未登记归属主体")
            return
        if info.owners:
            report.owners_registered = list(info.owners)
            return
        entries: list[Scope] = []
        for source in self.entry_scopes(org, space):
            entry = principal.owner_entry_of(source, org, space)
            if entry is None:
                continue
            if entry not in entries:
                entries.append(entry)
        if not entries:  # 条目 scope 已迁移过的空间：改从条目的作者标记推导
            entries = self._owners_from_marks(org, space)
        if not entries:
            report.unresolved.append("无法推导归属主体（无条目或主体维全空）")
            return
        if len(entries) > 1:
            report.unresolved.append(
                f"多归属空间，治理动作与整空间导出不放行：{[_render_scope(e) for e in entries]}"
            )
        report.owners_registered = entries
        if self._dry_run:
            return
        self._kv.update(
            _scope(org, space), _INFO_KEY, _info_to_bytes(replace(info, owners=entries))
        )

    def _owners_from_marks(self, org: str, space: str) -> list[Scope]:
        entries: list[Scope] = []
        for key, raw in self._kv.scan(_scope(org, space), prefix=MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is None:
                continue
            author = str((unit.system_metadata or {}).get(principal.AUTHOR_PRINCIPAL, ""))
            entry = _entry_from_author(author, org, space)
            if entry is not None and entry not in entries:
                entries.append(entry)
        return entries

    # -- 项 4：成员表合并 -------------------------------------------------- #

    def _merge_members(self, org: str, space: str, report: SpaceReport) -> None:
        """逐成员键并入单键并删除旧键；旧键已清空即空操作。"""
        scope = _scope(org, space)
        legacy = list(self._kv.scan(scope, prefix=_MEMBER_PREFIX))
        if not legacy:
            return
        # 以五维元组为键去重：``Scope`` 在本版本仍是可变 dataclass，不可哈希
        merged: dict[tuple[str, ...], SpaceMember] = {}
        try:
            for member in _members_from_bytes(self._kv.get(scope, _MEMBERS_KEY)):
                merged[_scope_key(member.scope)] = member  # 单键里的新格式记录优先
        except NotFoundError:
            pass
        for key, raw in legacy:
            member = _member_from_bytes(raw)
            merged.setdefault(_scope_key(member.scope), member)
        report.members_merged = len(legacy)
        if self._dry_run:
            return
        self._put(scope, _MEMBERS_KEY, _members_to_bytes(list(merged.values())))
        for key, _ in legacy:
            self._kv.delete(scope, key)

    # -- 主体反查索引 ------------------------------------------------------ #

    def rebuild_space_index(self, org: str, space: str, report: SpaceReport) -> None:
        """由归属登记与成员记录重建反查索引；索引项幂等，重复执行无副作用。"""
        principals: list[Scope] = list(report.owners_registered)
        scope = _scope(org, space)
        try:
            members = _members_from_bytes(self._kv.get(scope, _MEMBERS_KEY))
        except NotFoundError:
            members = []
        for member in members:
            if member.scope.user and member.scope.agent:
                continue  # 双维记录无对应索引桶，进形态审查
            if member.scope not in principals:
                principals.append(member.scope)
        report.index_entries = len(principals)
        if self._dry_run:
            return
        for entry in principals:
            # 与 KVSpaceManager 同一份键编码：本脚本已直接引用该模块的键常量与编解码，
            # 索引写入照此办理，不另起一套。写入幂等，重复执行无副作用。
            key = _index_key(_principal_bucket(entry), space)
            if not self._kv.exists(_ROOT_SCOPE, key):
                self._kv.insert(_ROOT_SCOPE, key, space.encode("utf-8"))

    # -- 存储与索引的写入封装 ---------------------------------------------- #

    def _put(self, scope: Scope, key: str, value: bytes) -> None:
        if self._dry_run:
            return
        if self._kv.exists(scope, key):
            self._kv.update(scope, key, value)
        else:
            self._kv.insert(scope, key, value)

    def _build_index(self, units: list[MemoryUnit]) -> None:
        if self._dry_run or self._index_builder is None:
            return
        self._index_builder.build(units)

    def _remove_index(self, units: list[MemoryUnit]) -> None:
        if self._dry_run or self._index_builder is None:
            return
        self._index_builder.remove(units)


def _entry_from_author(author: str, org: str, space: str) -> Scope | None:
    if author.startswith("user:") and len(author) > 5:
        return Scope(org=org, space=space, user=author[5:])
    if author.startswith("agent:") and len(author) > 6:
        return Scope(org=org, space=space, agent=author[6:])
    return None


# -- 配套子命令 ------------------------------------------------------------- #


def rebuild_registry(kv: KVStore, org: str, *, dry_run: bool = False) -> list[str]:
    """由存量空间重建 ``/spaces/by-id/`` 注册表。"""
    rebuilt: list[str] = []
    for scope in kv.scopes():
        if scope.org != org or not scope.space:
            continue
        if not kv.exists(_scope(scope.org, scope.space), _INFO_KEY):
            continue
        key = _registry_key(scope.space)
        if kv.exists(_ROOT_SCOPE, key):
            continue
        rebuilt.append(scope.space)
        if not dry_run:
            kv.insert(_ROOT_SCOPE, key, org.encode("utf-8"))
    return sorted(rebuilt)


def rebuild_index(kv: KVStore, org: str, *, dry_run: bool = False) -> dict[str, int]:
    """由成员记录与归属登记重建主体反查索引；与 ``backfill`` 分开，可单独重跑。"""
    backfiller = Backfiller(kv, dry_run=dry_run)
    counts: dict[str, int] = {}
    for space in backfiller.spaces_of(org):
        report = SpaceReport(org=org, space=space)
        try:
            info = _info_from_bytes(kv.get(_scope(org, space), _INFO_KEY))
            report.owners_registered = list(info.owners)
        except NotFoundError:
            continue
        backfiller.rebuild_space_index(org, space, report)
        counts[space] = report.index_entries
    return counts


def audit_shape(kv: KVStore, org: str) -> dict[str, list[str]]:
    """形态审查：列出需要人工处置的存量形态。

    第四类（``grantee.space`` 非空的存量授权）随上游授权记录存储合入后补——当前仓库的
    授权记录仍由控制层权限算子持有，该算子在改造后退出请求授权路径。
    """
    findings: dict[str, list[str]] = {
        "multi_owner_spaces": [],
        "member_with_session": [],
        "member_with_both_principal_dims": [],
        "unmigrated_entry_scopes": [],
    }
    backfiller = Backfiller(kv)
    for space in backfiller.spaces_of(org):
        scope = _scope(org, space)
        try:
            info = _info_from_bytes(kv.get(scope, _INFO_KEY))
        except NotFoundError:
            continue
        if len(info.owners) > 1:
            findings["multi_owner_spaces"].append(
                f"{space}: {[_render_scope(owner) for owner in info.owners]}"
            )
        members: list[SpaceMember] = []
        try:
            members = _members_from_bytes(kv.get(scope, _MEMBERS_KEY))
        except NotFoundError:
            pass
        for _, raw in kv.scan(scope, prefix=_MEMBER_PREFIX):
            members.append(_member_from_bytes(raw))
        for member in members:
            if member.scope.session:
                findings["member_with_session"].append(f"{space}: {_render_scope(member.scope)}")
            if member.scope.user and member.scope.agent:
                findings["member_with_both_principal_dims"].append(
                    f"{space}: {_render_scope(member.scope)}"
                )
        for entry_scope in backfiller.entry_scopes(org, space):
            if entry_scope != _target_scope(entry_scope):
                findings["unmigrated_entry_scopes"].append(_render_scope(entry_scope))
    return findings


def provision_main_spaces(
    kernel: _Kernel, org: str, principals: Iterable[str], *, dry_run: bool = False
) -> list[str]:
    """批量为清单内的主体预建主空间并登记归属。

    清单每行一个主体：``user:<id>`` 或 ``agent:<id>``；空行与 ``#`` 开头的行忽略。
    空间名取 ``u-<id>`` / ``a-<id>``。改造后建空间由角色闸门裁决、最终用户不再自建，
    其主空间由本子命令或开通服务预建；不新增 API 入口。
    """
    if kernel.space is None:
        raise RuntimeError("kernel has no SpaceManager; check the assembly config")
    created: list[str] = []
    for raw in principals:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        owner = _entry_from_author(line, org, "")
        if owner is None:
            logger.warning("provision-main-space: 无法解析主体 %r，跳过", line)
            continue
        space = f"u-{owner.user}" if owner.user else f"a-{owner.agent}"
        created.append(space)
        if dry_run:
            continue
        # owner 的 space 维由 create 内部按目标空间重建，此处不必预置
        kernel.space.create(SpaceSpec(org=org, space=space, display_name=space, owner=owner))
    return created


# -- CLI -------------------------------------------------------------------- #


def _load_config(config_path: str | None) -> Config | None:
    return Config.from_yaml(config_path) if config_path else None


def _index_builder_of(config: Config | None) -> IndexBuilder | None:
    """取检索索引构建器：与服务同一份配置下的 ``constructor.default`` 具名实例。

    条目 scope 迁移会改变检索索引的主键与图命名空间，旧文档不会被新写入覆盖，因此
    迁移须同时按新 scope 重建索引文档、按旧 scope 删除旧文档。本函数须在
    :func:`build_kernel` 之后调用——具名实例缓存此时已建立，取到的是与内核同一个
    存储后端实例。装配不出索引构建器时返回 ``None``，迁移只搬真源，索引由部署侧另行重建。
    """
    ctx = default_context()
    if config is not None and not config.is_empty():
        ctx = ctx.merged(config.context(known_top_names=Factory.known_top_names()))
    try:
        builder = IndexBuilderProducer.build_named(_ASSEMBLY_DEFAULT_NAME, ctx)
    except Exception:  # 装配失败不阻断真源迁移
        logger.warning("backfill: 索引构建器装配失败，条目迁移只搬真源", exc_info=True)
        return None
    return builder if isinstance(builder, IndexBuilder) else None


def _echo(line: str) -> None:
    """输出子命令结果。

    结果经 ``logger`` 而非 stdout 输出：迁移工具在运维环境中由调度器拉起，结果与运行
    日志需落同一通道才可被统一采集；直写 stdout 的部分在输出重定向丢失时无从追溯。
    """
    logger.info("%s", line)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backfill", description=__doc__)
    parser.add_argument("--config", default=None, help="与服务同一份装配配置（YAML）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backfill = sub.add_parser("backfill", help="五项回填")
    p_backfill.add_argument("--org", required=True)
    p_backfill.add_argument("--space", default=None, help="缺省即该 org 下全部空间")
    p_backfill.add_argument("--dry-run", action="store_true")

    p_provision = sub.add_parser("provision-main-space", help="批量预建主空间")
    p_provision.add_argument("--org", required=True)
    p_provision.add_argument("--principals", required=True, help="主体清单文件")
    p_provision.add_argument("--dry-run", action="store_true")

    p_registry = sub.add_parser("rebuild-registry", help="重建空间注册表")
    p_registry.add_argument("--org", required=True)
    p_registry.add_argument("--dry-run", action="store_true")

    p_index = sub.add_parser("rebuild-index", help="重建主体反查索引")
    p_index.add_argument("--org", required=True)
    p_index.add_argument("--dry-run", action="store_true")

    p_audit = sub.add_parser("audit-shape", help="形态审查")
    p_audit.add_argument("--org", required=True)

    args = parser.parse_args(argv)
    config = _load_config(args.config)
    kernel = _build_kernel(config=config)
    kv = kernel.kv

    if args.command == "backfill":
        backfiller = Backfiller(
            kv,
            index_builder=_index_builder_of(config),
            tag_keys=kernel.api.route_table.tag_keys,
            dry_run=args.dry_run,
        )
        spaces = [args.space] if args.space else backfiller.spaces_of(args.org)
        for space in spaces:
            _echo(backfiller.backfill_space(args.org, space).render())
        return 0

    if args.command == "provision-main-space":
        with open(args.principals, encoding="utf-8") as handle:
            created = provision_main_spaces(kernel, args.org, handle, dry_run=args.dry_run)
        _echo(f"预建主空间 {len(created)} 个：{created}")
        return 0

    if args.command == "rebuild-registry":
        rebuilt = rebuild_registry(kv, args.org, dry_run=args.dry_run)
        _echo(f"重建注册表 {len(rebuilt)} 项：{rebuilt}")
        return 0

    if args.command == "rebuild-index":
        counts = rebuild_index(kv, args.org, dry_run=args.dry_run)
        _echo(f"重建反查索引：{counts}")
        return 0

    findings = audit_shape(kv, args.org)
    for name, items in findings.items():
        _echo(f"{name}: {items or '无'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
