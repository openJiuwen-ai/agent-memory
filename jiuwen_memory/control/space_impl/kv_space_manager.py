# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""KV-backed SpaceManager implementation."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security import principal
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, MESSAGES_KEY_PREFIX, Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.space import SpaceManager, SpaceProducer
from jiuwen_memory.control.types import (
    PrincipalPath,
    SpaceDeleteResult,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
    SpaceUsage,
)
from jiuwen_memory.storage.storage import Storage, StorageProducer

logger = get_logger(__name__)

_INFO_KEY = "/space/info"
# 成员表整表落单键。改造前逐成员一个键、``list_members`` 靠 ``scan`` 取回，而判定链每次
# 求值都要读一次成员表：Redis 的 scan 是服务端遍历全 keyspace，Postgres 的前缀匹配退化为
# 扫该 scope 分区全部行（条目 scope 迁移后该分区即整个空间的全部记忆条目），两者的成本
# 都与该空间成员数无关。单键化后成员读取是一次点读。
_MEMBERS_KEY = "/space/members"
_MEMBER_PREFIX = "/space/members/"  # 存量逐成员键，供回填合并；读取侧不再回落到它
_MEMBERS_LIMIT = 1000  # 单空间成员数上限
_EXPORT_PREFIX = "/space/exports/"
_REGISTRY_PREFIX = "/spaces/by-id/"
# 主体到空间的反查索引前缀。与 ``/spaces/by-id/`` 注册表同处 KV 根 scope 桶：两者都是
# 跨 org 的全局注册数据，不属于任一空间的命名空间。主体与 org 编在键里，不靠 scope 列区分。
_INDEX_PREFIX = "/index/principal/"
_ROOT_SCOPE = Scope()

# space 状态流转白名单。DELETING / DELETED 只由删除流程内部置入，``update`` 不接受。
_STATUS_TRANSITIONS: dict[SpaceStatus, set[SpaceStatus]] = {
    SpaceStatus.ACTIVE: {SpaceStatus.FROZEN, SpaceStatus.ARCHIVED},
    SpaceStatus.FROZEN: {SpaceStatus.ACTIVE, SpaceStatus.ARCHIVED},
    SpaceStatus.ARCHIVED: {SpaceStatus.ACTIVE, SpaceStatus.FROZEN},
}

# 存量单轴角色到两轴的固定映射
_LEGACY_ROLE_MAP: dict[str, tuple[SpaceContentRole, SpaceGovernanceRole]] = {
    "owner": (SpaceContentRole.EDITOR, SpaceGovernanceRole.OWNER),
    "admin": (SpaceContentRole.EDITOR, SpaceGovernanceRole.MANAGER),
    "member": (SpaceContentRole.CONTRIBUTOR, SpaceGovernanceRole.NONE),
    "viewer": (SpaceContentRole.VIEWER, SpaceGovernanceRole.NONE),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope(org: str, space: str) -> Scope:
    return Scope(org=org, space=space)


def _validate_space(org: str, space: str) -> None:
    if not org:
        raise ValidationError("org is required")
    if not space:
        raise ValidationError("space is required")


def _dt(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _scope_to_dict(scope: Scope) -> dict[str, str]:
    return {
        "org": scope.org,
        "space": scope.space,
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _scope_from_dict(data: dict[str, str]) -> Scope:
    return Scope(
        org=str(data.get("org", "")),
        space=str(data.get("space", "")),
        user=str(data.get("user", "")),
        agent=str(data.get("agent", "")),
        session=str(data.get("session", "")),
    )


def _policy_to_dict(policy: SpacePolicy) -> dict[str, object]:
    return {
        "require_space": policy.require_space,
        "principal_path": policy.principal_path.value,
        "storage_isolation_strategy": policy.storage_isolation_strategy,
        "retention": dict(policy.retention),
        "quotas": dict(policy.quotas),
        "index_profiles": dict(policy.index_profiles),
        "pipeline_profiles": dict(policy.pipeline_profiles),
    }


def _policy_from_dict(data: dict[str, object] | None) -> SpacePolicy:
    data = data or {}
    return SpacePolicy(
        require_space=bool(data.get("require_space", False)),
        principal_path=PrincipalPath(
            str(data.get("principal_path", PrincipalPath.USER_AGENT.value))
        ),
        storage_isolation_strategy=str(data.get("storage_isolation_strategy", "metadata_filter")),
        retention={str(k): str(v) for k, v in dict(data.get("retention", {}) or {}).items()},
        quotas={str(k): str(v) for k, v in dict(data.get("quotas", {}) or {}).items()},
        index_profiles={
            str(k): str(v) for k, v in dict(data.get("index_profiles", {}) or {}).items()
        },
        pipeline_profiles={
            str(k): str(v) for k, v in dict(data.get("pipeline_profiles", {}) or {}).items()
        },
    )


def _info_to_bytes(info: SpaceInfo) -> bytes:
    payload = {
        "org": info.org,
        "space": info.space,
        "display_name": info.display_name,
        "status": info.status.value,
        "principal_path": info.principal_path.value,
        "policy": _policy_to_dict(info.policy),
        "metadata": dict(info.metadata),
        "created_at": _dt(info.created_at),
        "archived_at": _dt(info.archived_at),
        # 归属登记复用现有 Scope 编码，不引入第二套。本编解码是手写的逐字段映射，
        # 漏列即 owners 在序列化处被丢弃、读回恒为空，归属对比整体不生效。
        "owners": [_scope_to_dict(owner) for owner in info.owners],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _info_from_bytes(raw: bytes) -> SpaceInfo:
    data = json.loads(raw.decode("utf-8"))
    policy = _policy_from_dict(data.get("policy"))
    principal_path = PrincipalPath(str(data.get("principal_path", policy.principal_path.value)))
    policy = replace(policy, principal_path=principal_path)
    return SpaceInfo(
        org=str(data.get("org", "")),
        space=str(data.get("space", "")),
        display_name=str(data.get("display_name", "")),
        status=SpaceStatus(str(data.get("status", SpaceStatus.ACTIVE.value))),
        principal_path=principal_path,
        policy=policy,
        metadata={str(k): str(v) for k, v in dict(data.get("metadata", {}) or {}).items()},
        created_at=_parse_dt(data.get("created_at")),
        archived_at=_parse_dt(data.get("archived_at")),
        owners=[
            _scope_from_dict(dict(owner) or {}) for owner in list(data.get("owners", []) or [])
        ],
    )


def _roles_from_dict(data: dict) -> tuple[SpaceContentRole, SpaceGovernanceRole]:
    """解析成员记录的两轴角色，兼容只带单轴 ``role`` 的存量记录。"""
    if "content_role" in data or "governance_role" in data:  # 新格式直读
        return (
            SpaceContentRole(str(data.get("content_role", SpaceContentRole.NONE.value))),
            SpaceGovernanceRole(str(data.get("governance_role", SpaceGovernanceRole.NONE.value))),
        )
    legacy = str(data.get("role", "member"))
    if legacy not in _LEGACY_ROLE_MAP:
        # 未识别取值按最低档处置而非抛异常：拒绝解析会使存量空间整体不可用。
        logger.warning("space member: unknown legacy role %r, degraded to viewer", legacy)
        return (SpaceContentRole.VIEWER, SpaceGovernanceRole.NONE)
    return _LEGACY_ROLE_MAP[legacy]


def _member_to_dict(member: SpaceMember) -> dict[str, object]:
    return {
        "scope": _scope_to_dict(member.scope),
        "role": member.role,
        # 两个新字段须在写侧显式列举，读侧由 _roles_from_dict 承担
        "content_role": member.content_role.value,
        "governance_role": member.governance_role.value,
        "created_at": _dt(member.created_at),
        "expires_at": _dt(member.expires_at),
    }


def _member_from_dict(data: dict) -> SpaceMember:
    content_role, governance_role = _roles_from_dict(data)
    return SpaceMember(
        scope=_scope_from_dict(dict(data.get("scope", {}) or {})),
        role=str(data.get("role", "member")),
        content_role=content_role,
        governance_role=governance_role,
        created_at=_parse_dt(data.get("created_at")),
        expires_at=_parse_dt(data.get("expires_at")),
    )


def _member_from_bytes(raw: bytes) -> SpaceMember:
    """解析一条存量的逐成员键记录；供回填脚本合并旧键使用。"""
    return _member_from_dict(json.loads(raw.decode("utf-8")))


def _members_to_bytes(members: list[SpaceMember]) -> bytes:
    payload = [_member_to_dict(member) for member in members]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _members_from_bytes(raw: bytes) -> list[SpaceMember]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValidationError("space members payload must be a list")
    return [_member_from_dict(dict(item or {})) for item in data]


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _registry_key(space: str) -> str:
    return f"{_REGISTRY_PREFIX}{_b64(space)}"


def _principal_bucket(scope: Scope) -> str:
    """索引桶键三类：具名 user、具名 agent、组织通配（主体两维皆空的成员记录）。

    双维主体在此拒绝，理由见 :meth:`SpaceManager.add_member`。
    """
    if scope.user and scope.agent:
        raise ValidationError("index principal must carry exactly one of user/agent")
    if scope.user:
        return f"u:{scope.org}:{scope.user}"
    if scope.agent:
        return f"a:{scope.org}:{scope.agent}"
    return f"org:{scope.org}"


def _index_key(bucket: str, space: str) -> str:
    return f"{_INDEX_PREFIX}{_b64(bucket)}/{_b64(space)}"


def _normalize_member(org: str, space: str, member: SpaceMember) -> SpaceMember:
    return SpaceMember(
        scope=_normalize_member_scope(org, space, member.scope),
        role=member.role or "member",
        content_role=member.content_role,
        governance_role=member.governance_role,
        created_at=member.created_at or _now(),
        expires_at=member.expires_at,
    )


def _normalize_member_scope(org: str, space: str, member: Scope) -> Scope:
    if member.org and member.org != org:
        raise ValidationError("member scope org must match target space org")
    if member.space and member.space != space:
        raise ValidationError("member scope space must match target space")
    return replace(member, org=org, space=space)


def _check_status_transition(current: SpaceStatus, target: SpaceStatus) -> None:
    """状态流转校验：同态为空操作，非白名单流转即拒绝。

    ``DELETING`` / ``DELETED`` 只由删除流程内部置入，管理面 ``update`` 不接受。
    """
    if current == target:
        return
    allowed = _STATUS_TRANSITIONS.get(current)
    if allowed is None:
        raise ValidationError(f"space in status {current.value!r} cannot be updated")
    if target not in allowed:
        raise ValidationError(
            f"space status transition {current.value!r} -> {target.value!r} is not allowed"
        )


class KVSpaceManager(SpaceManager):
    """SpaceManager backed by KVStore."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._kv = storage.kv

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.SPACE

    def health(self) -> None:
        self._kv.health()

    def create(self, spec: SpaceSpec) -> SpaceInfo:
        _validate_space(spec.org, spec.space)
        scope = _scope(spec.org, spec.space)
        registry_key = _registry_key(spec.space)
        if self._kv.exists(_ROOT_SCOPE, registry_key):
            raise ConflictError("space", spec.space)
        for candidate in self._kv.scopes():
            if candidate.space != spec.space:
                continue
            info_scope = _scope(candidate.org, candidate.space)
            if self._kv.exists(info_scope, _INFO_KEY):
                raise ConflictError("space", spec.space)
        if self._kv.exists(scope, _INFO_KEY):
            raise ConflictError("space", spec.space)
        policy = replace(spec.policy, principal_path=spec.principal_path)
        owner_entry = (
            None
            if spec.owner is None
            else principal.owner_entry_of(spec.owner, spec.org, spec.space)
        )
        info = SpaceInfo(
            org=spec.org,
            space=spec.space,
            display_name=spec.display_name or spec.space,
            status=SpaceStatus.ACTIVE,
            principal_path=spec.principal_path,
            policy=policy,
            metadata={str(k): str(v) for k, v in spec.metadata.items()},
            created_at=_now(),
            owners=[] if owner_entry is None else [owner_entry],
        )
        self._kv.insert(_ROOT_SCOPE, registry_key, spec.org.encode("utf-8"))
        try:
            self._kv.insert(scope, _INFO_KEY, _info_to_bytes(info))
            for entry in info.owners:  # 主数据先、索引后
                self._index_add(entry, spec.space)
        except Exception:
            self._rollback_create(scope, registry_key, info.owners)
            raise
        return info

    def _rollback_create(self, scope: Scope, registry_key: str, owners: list[Scope]) -> None:
        """创建失败的回滚：索引项、空间元数据、注册表逐一撤销（各自幂等）。"""
        for entry in owners:
            try:
                self._index_remove(entry, scope.space)
            except Exception:  # 索引孤儿项只造成候选集虚大，不阻断回滚
                logger.warning(
                    "space create rollback: index entry left behind for %s/%s",
                    scope.org,
                    scope.space,
                )
        self._kv.delete(scope, _INFO_KEY)
        self._kv.delete(_ROOT_SCOPE, registry_key)

    def get(self, org: str, space: str) -> SpaceInfo:
        _validate_space(org, space)
        try:
            return _info_from_bytes(self._kv.get(_scope(org, space), _INFO_KEY))
        except NotFoundError:
            raise NotFoundError("space", f"{org}/{space}") from None

    def list(
        self,
        org: str,
        *,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        if not org:
            raise ValidationError("org is required")
        if limit <= 0:
            raise ValidationError("limit must be > 0")
        try:
            offset = int(cursor or "0")
        except ValueError:
            raise ValidationError(f"cursor must be an integer offset, got {cursor!r}") from None
        if offset < 0:
            raise ValidationError("cursor must be >= 0")

        spaces: dict[tuple[str, str], SpaceInfo] = {}
        for scope in self._kv.scopes():
            if scope.org != org or not scope.space:
                continue
            try:
                info = self.get(scope.org, scope.space)
            except NotFoundError:
                continue
            if status is not None and info.status != status:
                continue
            spaces[(info.org, info.space)] = info
        ordered = [spaces[key] for key in sorted(spaces)]
        return ordered[offset:offset + limit]

    def update(self, org: str, space: str, patch: SpacePatch) -> SpaceInfo:
        info = self.get(org, space)
        if patch.status is not None:
            _check_status_transition(info.status, patch.status)
        if patch.display_name is not None:
            info.display_name = patch.display_name
        if patch.policy is not None:
            info.policy = patch.policy
            info.principal_path = patch.policy.principal_path
        if patch.principal_path is not None:
            info.principal_path = patch.principal_path
            info.policy = replace(info.policy, principal_path=patch.principal_path)
        if patch.metadata is not None:
            info.metadata.update({str(k): str(v) for k, v in patch.metadata.items()})
        if patch.status is not None:
            info.status = patch.status
            if patch.status == SpaceStatus.ARCHIVED and info.archived_at is None:
                info.archived_at = _now()
        self._kv.update(_scope(org, space), _INFO_KEY, _info_to_bytes(info))
        return info

    def archive(self, org: str, space: str) -> SpaceInfo:
        return self.update(org, space, SpacePatch(status=SpaceStatus.ARCHIVED))

    def delete(self, org: str, space: str) -> SpaceDeleteResult:
        self.get(org, space)
        # 先清索引后删主数据：孤儿索引项会被逐空间判定挡住，只造成候选集虚大；
        # 反过来则出现「空间已删、索引仍指向它」之外的更坏形态——主数据已删而索引清理
        # 中断时无从重建清理依据。
        self._index_remove_space(space)
        counts = {
            "memory": 0,
            "message": 0,
            "space_metadata": 0,
            "kv": 0,
            "index": 0,
        }
        target_scopes = [
            scope for scope in self._kv.scopes() if scope.org == org and scope.space == space
        ]
        if not target_scopes:
            target_scopes = [_scope(org, space)]
        for scope in target_scopes:
            for key, _ in list(self._kv.scan(scope)):
                if key.startswith(MEMORY_KEY_PREFIX):
                    counts["memory"] += 1
                elif key.startswith(MESSAGES_KEY_PREFIX):
                    counts["message"] += 1
                elif key.startswith("/space/"):
                    counts["space_metadata"] += 1
                counts["kv"] += 1
                self._kv.delete(scope, key)
        registry_key = _registry_key(space)
        if self._kv.exists(_ROOT_SCOPE, registry_key):
            self._kv.delete(_ROOT_SCOPE, registry_key)
            counts["space_metadata"] += 1
            counts["kv"] += 1
        return SpaceDeleteResult(org=org, space=space, deleted_counts=counts)

    def export(self, org: str, space: str, *, include_audit: bool = True) -> str:
        info = self.get(org, space)
        export_id = str(uuid.uuid4())
        payload = {
            "id": export_id,
            "org": org,
            "space": space,
            "include_audit": include_audit,
            "created_at": _dt(_now()),
            "status": "created",
            "space_status": info.status.value,
        }
        self._kv.insert(
            _scope(org, space),
            f"{_EXPORT_PREFIX}{export_id}",
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        return export_id

    def usage(self, org: str, space: str) -> SpaceUsage:
        _validate_space(org, space)
        usage = SpaceUsage(org=org, space=space)
        for scope in self._kv.scopes():
            if scope.org != org or scope.space != space:
                continue
            for key, value in self._kv.scan(scope):
                usage.storage_bytes += len(value)
                if key.startswith(MEMORY_KEY_PREFIX):
                    usage.memory_count += 1
                elif key.startswith(MESSAGES_KEY_PREFIX):
                    usage.message_count += 1
        return usage

    def get_policy(self, org: str, space: str) -> SpacePolicy:
        return self.get(org, space).policy

    def set_policy(self, org: str, space: str, policy: SpacePolicy) -> SpacePolicy:
        info = self.update(
            org,
            space,
            SpacePatch(policy=policy, principal_path=policy.principal_path),
        )
        return info.policy

    def list_members(self, org: str, space: str) -> list[SpaceMember]:
        self.get(org, space)
        members = self._read_members(org, space)
        members.sort(
            key=lambda member: (
                member.scope.user,
                member.scope.agent,
                member.scope.session,
                member.role,
            )
        )
        return members

    def _read_members(self, org: str, space: str) -> list[SpaceMember]:
        """读成员表：一次点读，不做空间存在性校验。

        存量的逐成员键不在此回落——保留回落等于让那次 ``scan`` 在未迁移空间上长期存在。
        旧键由回填脚本合并为单键（``backfill`` 的成员表合并项）。
        """
        try:
            return _members_from_bytes(self._kv.get(_scope(org, space), _MEMBERS_KEY))
        except NotFoundError:
            return []  # 无成员记录即个体空间

    def _write_members(self, org: str, space: str, members: list[SpaceMember]) -> None:
        """整表写回：单键写入天然原子，无需多键事务。

        并发写以后写覆盖结束，与现有 ``update`` 对空间元数据的写入形态一致——成员变更是
        低频管理动作，不引入 CAS。
        """
        if len(members) > _MEMBERS_LIMIT:
            raise ValidationError(f"space members exceed the limit of {_MEMBERS_LIMIT}")
        payload = _members_to_bytes(members)
        scope = _scope(org, space)
        if self._kv.exists(scope, _MEMBERS_KEY):
            self._kv.update(scope, _MEMBERS_KEY, payload)
        else:
            self._kv.insert(scope, _MEMBERS_KEY, payload)

    def add_member(self, org: str, space: str, member: SpaceMember) -> None:
        info = self.get(org, space)
        normalized = _normalize_member(org, space, member)
        if normalized.scope.session:
            raise ValidationError("member scope must not carry session")
        if normalized.scope.user and normalized.scope.agent:
            # 主体维取单维：双维记录在两轴求值时于两维上同时命中，取交退化为与自身相交，
            # 「两维各自约束」的语义消失；反查索引也不设双维桶。
            raise ValidationError("member scope must carry exactly one of user/agent")
        existing = self._read_members(org, space)
        kept = [m for m in existing if m.scope != normalized.scope]
        to_write = [normalized]
        if not existing and info.owners:
            # 空间由个体转共享的瞬间：补写归属主体，否则它在成员表非空后失去归属对比这条
            # 通路，对自己的空间反而无权。
            owner_entry = info.owners[0]
            if owner_entry == normalized.scope:
                # 同 scope 合并为一条：成员记录以 scope 为唯一标识，写两条时后者覆盖前者，
                # 会静默丢掉归属主体的治理权。用户级共享空间正是这一形态。
                to_write = [replace(normalized, governance_role=SpaceGovernanceRole.OWNER)]
            else:
                to_write.insert(
                    0,
                    SpaceMember(
                        scope=owner_entry,
                        role="owner",
                        content_role=SpaceContentRole.EDITOR,
                        governance_role=SpaceGovernanceRole.OWNER,
                        created_at=_now(),
                    ),
                )
        self._write_members(org, space, [*kept, *to_write])  # 主数据先
        try:
            for written in to_write:  # 索引后
                self._index_add(written.scope, space)
        except Exception:
            # 索引写失败即回滚成员表：留下成员记录而无索引项是遗漏方向的失效，该成员在
            # 按主体反查空间时看不到这个空间。已写入的索引项不回收——多给方向由逐空间
            # 判定挡住，是索引契约允许的方向。
            self._write_members(org, space, existing)
            raise

    def remove_member(self, org: str, space: str, member: Scope) -> None:
        info = self.get(org, space)
        normalized = _normalize_member_scope(org, space, member)
        existing = self._read_members(org, space)
        # 与 add_member 同口径：主数据先、索引后。反过来时若写成员表失败，成员记录仍在
        # 而索引项已删，该成员按主体反查空间即看不到这个空间——遗漏是索引超集契约唯一
        # 禁止的方向。本次序下的失败留下孤儿索引项，属允许的多给方向，由逐空间判定挡住。
        self._write_members(org, space, [m for m in existing if m.scope != normalized])
        if normalized not in info.owners:
            # 归属主体的索引项不随成员身份移除而删：它凭归属登记仍持有归属主体档，
            # 而按主体反查空间的候选集取自索引，删掉即看不到自己的空间。
            self._index_remove(normalized, space)

    # -- 主体反查索引 ------------------------------------------------------- #
    #
    # 索引是派生数据，不是第二份真源：成员表与归属登记按空间存放（``/space/members``、
    # ``/space/info``），能点读「这个空间里有谁」；反方向「某主体在哪些空间」在这个布局
    # 下只能遍历全库 scope 逐个翻成员表（``list`` 现在就是这么做的）。KV 没有二级索引，
    # 一份数据只能按一个键查，因此在根 scope 桶里另存一份按主体组织的键。
    #
    # 增删成员与登记归属一律「主数据先、索引后」：反过来时中断会留下「成员记录还在、索引项
    # 没了」，即超集语义唯一禁止的遗漏方向。删整个空间是例外，先清索引后删空间数据——
    # 空间数据随后整体消失，遗漏不成立，而反过来中断会留下指向已删空间的索引项。

    def spaces_for(self, actor: Scope, org: str) -> tuple[str, ...]:
        """契约见 :meth:`SpaceManager.spaces_for`。

        三路合并：actor 的 user 桶、agent 桶与组织通配桶。actor 带两维时两个桶都取，
        取并集而非交集——索引只负责不遗漏。
        """
        spaces: set[str] = set()
        buckets = [f"org:{org}"]
        if actor.user:
            buckets.append(f"u:{org}:{actor.user}")
        if actor.agent:
            buckets.append(f"a:{org}:{actor.agent}")
        for bucket in buckets:
            prefix = f"{_INDEX_PREFIX}{_b64(bucket)}/"
            for _key, raw in self._kv.scan(_ROOT_SCOPE, prefix=prefix):
                spaces.add(raw.decode("utf-8"))
        return tuple(sorted(spaces))

    def _index_add(self, subject: Scope, space: str) -> None:
        """登记一条「主体 → 空间」；幂等：已存在即跳过。"""
        if not space:
            raise ValidationError("space is required")
        key = _index_key(_principal_bucket(subject), space)
        if self._kv.exists(_ROOT_SCOPE, key):
            return
        self._kv.insert(_ROOT_SCOPE, key, space.encode("utf-8"))

    def _index_remove(self, subject: Scope, space: str) -> None:
        """删除一条「主体 → 空间」；幂等：不存在即跳过。"""
        if not space:
            return
        self._kv.delete(_ROOT_SCOPE, _index_key(_principal_bucket(subject), space))

    def _index_remove_space(self, space: str) -> int:
        """清理某个空间的全部索引项，返回删除条数。

        用于删除空间：孤儿项只造成候选集虚大，会被逐空间判定挡住，因此清理失败可容忍。
        """
        if not space:
            return 0
        suffix = f"/{_b64(space)}"
        removed = 0
        for key, _raw in self._kv.scan(_ROOT_SCOPE, prefix=_INDEX_PREFIX):
            if not key.endswith(suffix):
                continue
            self._kv.delete(_ROOT_SCOPE, key)
            removed += 1
        return removed


@SpaceProducer.register("kv")
def _build(config):
    return KVSpaceManager(StorageProducer.resolve(config))
