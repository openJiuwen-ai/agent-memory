# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared LocalMemoryAPI helpers (validation, filters, space projections)."""

from __future__ import annotations

from dataclasses import fields, replace
from math import isfinite
from typing import Any

from jiuwen_memory.common.errors import (
    PolicyError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger, metadata_for_log
from jiuwen_memory.common.security import principal, space_predicates
from jiuwen_memory.common.security.space_roles import (
    ENTRY_RULES,
    SpaceAction,
    SpaceAuthorizationFacts,
    SpaceAxis,
    SpaceMemberFact,
)
from jiuwen_memory.common.security.types import Action, Grant
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    EXT_SPACES,
    KERNEL_SYSTEM_METADATA_KEYS,
    TRANSIENT_SYSTEM_METADATA_KEYS,
    ChannelError,
    Context,
    FilterClause,
    FilterExpr,
    FilterOp,
    MetadataValueType,
    Scope,
    canonical_filter_field,
    extract_required_equality,
    filter_field_metadata_key,
    iter_clauses,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control.policy import PolicyManager
from jiuwen_memory.control.types import (
    DeleteSelector,
    PermissionContext,
    SpaceFacts,
    SpaceInfo,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
)
from jiuwen_memory.retrieval.cross_space import space_error

logger = get_logger("jiuwen_memory.api.memory_api_impl.local_memory_api")

# 管理面（admin / 全局审计）没有具体 target scope，统一以「根 scope」
# 为鉴权目标：在真实 RBAC 后端下，「能对全局根 scope 行权」即等价于
# 管理员闸门；在 allow_all
# 装配下为 no-op。租户数据/治理方法仍按各自的 target scope 鉴权。
_ROOT = Scope()

# ``list_spaces`` 取候选时的全库扫描上界。逐空间判权的次数等于候选数，需有一个显式的
# 天花板；达到该值记 WARNING，不静默截断。
_SPACE_SCAN_CAP = 10000

# 授权记录动作到空间动作枚举的反向映射，供显式授权的授出上界校验使用。
# 与判定宿主的正向映射一一对应：五值枚举中的每一项在内容轴上都有同名项。
_GRANT_ACTION_BACK: dict[Action, SpaceAction] = {
    Action.READ: SpaceAction.READ,
    Action.WRITE: SpaceAction.WRITE,
    Action.UPDATE: SpaceAction.UPDATE,
    Action.DELETE: SpaceAction.DELETE,
    Action.SHARE: SpaceAction.SHARE,
}


_LEGACY_PERMISSION_ACTIONS = frozenset(
    {Action.READ, Action.WRITE, Action.UPDATE, Action.DELETE, Action.SHARE}
)


def _validate_legacy_permission_actions(grant: Grant) -> None:
    """旧 PermissionManager 未实现安全域管理动作，过渡期对这些动作 fail-closed。"""
    unsupported = grant.actions - _LEGACY_PERMISSION_ACTIONS
    if unsupported:
        values = ", ".join(sorted(action.value for action in unsupported))
        raise ValueError(f"legacy PermissionManager does not support actions: {values}")


def _parse_max_tokens(raw: str | None) -> int | None:
    """解析 ``Context.extensions`` 的约定 key ``max_tokens`` 为披露预算（int）。

    缺失/空串 → ``None``（披露阶段用默认策略）；非整数 →
    :class:`~common.errors.ValidationError`
    （可预期的调用错误，与 ``RetrievalQuery`` 的 ``max_tokens<=0`` 校验同档）。
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"max_tokens must be an integer, got {raw!r}") from None


def _context_detail(context: PermissionContext | None) -> dict[str, str]:
    if context is None:
        return {}
    detail = {
        "permission_resource_type": context.resource_type,
        "permission_memory_type": context.memory_type,
        "permission_pipeline": context.pipeline,
        "permission_unit_id": context.unit_id,
    }
    if context.tags:
        detail["permission_tags"] = ",".join(context.tags)
    if context.scope.space:
        detail["permission_space"] = context.scope.space
    return {key: value for key, value in detail.items() if value}


def _required_filter_metadata(filters: FilterExpr | None) -> dict[str, Any]:
    """提取 FilterExpr 逻辑上强制的唯一等值，作为查询路由的 filter 侧候选值。

    ``_recall_permission_context`` 随后按 S03「MemoryPipeline 路由规则」让
    ``Context.extensions`` 中的非空值覆盖本结果，使权限路由与 Pipeline 执行路由同源。
    OR 多值、NOT、AND 冲突和未限定字段不会从 filters 产出候选值；若 extensions 也未
    提供，则权限路由进入最小权限 ``fallback``。
    """
    routed: dict[str, str] = {}
    ambiguous: set[str] = set()
    clause_fields = {clause.field for clause in iter_clauses(filters)}
    for field in clause_fields:
        value = extract_required_equality(filters, field)
        if value is None:
            continue
        normalized = str(value).strip()
        key = filter_field_metadata_key(field).removeprefix("system_metadata.")
        if not normalized or key in ambiguous:
            continue
        if key in routed and routed[key] != normalized:
            routed.pop(key)
            ambiguous.add(key)
            continue
        routed[key] = normalized
    return routed


def _reject_invalid_content(content: object) -> None:
    """写入边界拒绝非 str / 空 / 纯空白 content。

    单条 write 曾把校验漏到 engine ``content.encode`` 与 Normalizer：``None`` 变
    ``AttributeError``，``""`` 靠 ``b""`` 假值副作用才成 ValidationError，``"   "``
    则静默落盘。与 batch 路径统一在 API 入口失败响亮。
    """
    if not isinstance(content, str):
        raise ValidationError(f"content must be str, got {type(content).__name__}")
    if not content.strip():
        raise ValidationError("content must be a non-empty str")


def _resolve_space_owner(spec: SpaceSpec, identity: Scope) -> SpaceSpec:
    """定妥建空间时的归属登记项，三种形态各走一条分支（F07「空间开通契约」）。

    - ``owner is None``：调用方没表态，按「谁建的空间归谁」填入调用方身份。少这一步，
      调用方给自己建的空间没有归属主体，而个体空间靠归属登记裁决——他自己也访问不了，
      且建空间那次不报错，下一次访问才失败。
    - ``owner`` 主体维全空：显式声明不登记，共享空间取此形态。其治理权由成员记录的
      治理轴与组织管理员的治理放行承担，不能让执行建空间动作的运维身份长期持有归属主体档。
    - ``owner`` 具名：替该主体登记，用于替某个用户建他的主空间。

    后两种形态由调用方填写，因此在此校验。两条都放到 API 层做前置：主体维双维非空虽然
    也会在反查索引写入处被拒，但那时主数据已写、要靠回滚收拾，且错误信息落在索引层。
    """
    if spec.owner is None:
        return replace(spec, owner=identity)
    if spec.owner.org and spec.owner.org != spec.org:
        raise ValidationError(
            f"space owner must belong to org {spec.org!r}, got {spec.owner.org!r}"
        )
    if spec.owner.user and spec.owner.agent:
        raise ValidationError("space owner must carry exactly one of user/agent")
    return spec


def _reject_kernel_system_metadata(metadata: dict[str, Any] | None) -> None:
    """写入/更新边界拒绝调用方占用内核系统元数据 key（F07）。

    ``system_metadata`` 是内核可解释的命名空间，同时也是对外入参。调用方能自行赋值这些
    key 即可伪造归属：作者主体写成他人的，或改写判定命中的类别名。用户元数据落在另一个
    命名空间、物理上占用不了这些 key，因此本校验只作用于 ``system_metadata`` 入参。
    """
    if not metadata:
        return
    clash = sorted(set(metadata) & KERNEL_SYSTEM_METADATA_KEYS)
    if clash:
        raise ValidationError(f"system_metadata 不得使用内核保留 key：{clash}")


def _truthy_metadata(metadata: dict[str, Any] | None, key: str) -> bool:
    """写入路径开关的取值判定（``infer`` / ``procedural`` / ``middle``）。

    真值集合与引擎侧 ``_is_true`` 逐字一致，只认 ``"true"``。放宽本层会让 ``middle="1"``
    这类取值在 API 层判为真、在真正据此分派的引擎层判为假，同一开关两层读法不同。
    """
    return str((metadata or {}).get(key, "")).strip().lower() == "true"


def _reject_route_tag_keys(metadata: dict[str, Any] | None, tag_keys: frozenset[str]) -> None:
    """写入边界拒绝调用方占用判定标签键（F07「写入边界校验」）。

    收窄维与记录维生成的键参与检索过滤，调用方能自行赋值即可绕过收窄——把一条内容的会话
    标签写成别人的会话 id，即让它出现在别人的上下文里。这些键不是静态保留键，而是装配期
    由判定表解析得出的运行时集合；未装配判定算子时集合为空，校验退化为空操作。
    """
    if not metadata or not tag_keys:
        return
    clash = sorted(set(metadata) & tag_keys)
    if clash:
        raise ValidationError(f"metadata 不得使用判定标签 key：{clash}")


def _reject_foreign_routed_scope(scope: Scope, identity: Scope) -> None:
    """走判定路径时对入参 ``scope`` 其余维的校验。

    判定路径的落点由内核算出，入参 ``scope`` 除 ``space`` 外的字段一概不参与计算——``org``
    取自身份，主体维在落盘 scope 上恒为空。不校验即静默丢弃调用方的声明：写 ``user=bob``
    会落进调用方自己的主空间，写另一个 ``org`` 会落进调用方所在的 org，两者都与声明不符
    且没有任何提示。

    ``space`` 维不在本函数的校验范围内：它在这条路径上同样不参与落点计算，但那是 ``coords``
    键的既定语义（交出落点决定权），不是被丢弃的声明，理由见 :meth:`_routes_by_decision`。

    直写路径的同名校验只管主体两维（``org`` 在那条路径上是真实落点的一部分、随 scope 一起
    生效）；本函数多校验 ``org``，因为在这条路径上它同样不生效。
    """
    _reject_foreign_write_scope(scope, identity)
    if scope.org and scope.org != identity.org:
        raise ValidationError(
            f"write scope org={scope.org!r} does not match the caller identity"
        )


def _reject_foreign_write_scope(scope: Scope, identity: Scope) -> None:
    """写入边界校验入参 scope 的**主体两维**：非空且与调用方身份不一致即拒绝。

    归属不由调用方声明。缺这一条时调用方可把条目写成另一个主体的：条目的作者标记由内核
    按身份写入、伪造不了，但落盘 scope 的主体维会成为第二条归属判据，两者指向不同主体。

    只校验 user 与 agent 两维。会话维不校验，由 :func:`_space_level_scope` 直接丢弃——它
    不是归属主体，写成别人的会话 id 不产生越权；会话相关性由收窄维标签承担，而那个键的
    取值来自调用方身份、不来自入参 scope。
    """
    for dim in ("user", "agent"):
        value = getattr(scope, dim)
        if value and value != getattr(identity, dim):
            raise ValidationError(
                f"write scope {dim}={value!r} does not match the caller identity"
            )


def _space_level_scope(scope: Scope) -> Scope:
    """条目落盘 scope 归一为 ``Scope(org, space)`` 两维。

    只在启用归属判定时调用：主体维与会话维留在落盘 scope 上时，判定第 8 步按各维相等放行，
    作者对自己写的条目取得全部动作权、不经内容轴矩阵。未启用判定的部署沿用原 scope，
    行为与改造前一致。
    """
    return Scope(org=scope.org, space=scope.space)


def _parse_coords(value: object) -> dict[str, str]:
    """归属坐标的运行期判型。

    形参形态下这层约束由类型注解承担，改走参数袋后须在此补回——注解管不到袋内的取值。
    覆盖不到的只有键名拼写：内核无从区分「键名拼错」与「本次不带该坐标」，两者在入参上
    是同一形态。
    """
    if not isinstance(value, dict):
        raise ValidationError(f"{COORDS_KEY} 必须是 dict[str, str]")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValidationError(f"{COORDS_KEY} 的键与值必须都是 str")
    return dict(value)


def _pop_coords(options: dict[str, Any], *, enabled: bool) -> dict[str, str] | None:
    """从检索侧的 ``extensions`` 副本取出归属坐标并移除。

    移除不可省略：``options`` 随 parser 透传给自定义检索模块，留下会让坐标以一个未声明
    的字段出现在该模块的入参里。与 ``EXT_MAX_TOKENS`` 同一处置。

    ``enabled`` 为假即整体不介入：未装配判定表的部署里该键不产生收窄谓词，取出它只会让
    自定义检索模块少收到一个调用方明明传了的字段。本特性在那种部署里应当完全不可达。
    """
    if not enabled or COORDS_KEY not in options:
        return None
    return _parse_coords(options.pop(COORDS_KEY))


def _pop_spaces(options: dict[str, Any]) -> list[str] | None:
    """从检索侧的 ``extensions`` 副本取出候选空间列表并移除；返回 ``None`` 表示单空间检索。

    **判据取键的有无，不取取值形态。** 键不在即单空间检索，:meth:`LocalMemoryAPI.search`
    的行为与本特性之前逐字一致；键在即跨空间检索，取值为空列表表示「调用方可读的全部
    空间」，由主体反查索引给出。若改按取值判空分流，「查我能读的全部」这层意图就只能靠
    缺省状态表达，与「没打算跨空间」不可区分。

    ``None`` 按非法取值拒绝，不当作空列表。二者的失效方向不对称：网关把未填字段序列化成
    ``null`` 时若按空列表处置，一次本意为单空间的检索会静默扩到调用方可读的全部空间。

    移除不可省略：``options`` 随 parser 透传给自定义检索模块，留下会让编排开关以一个未
    声明的字段出现在该模块的入参里。与 ``EXT_MAX_TOKENS``、``COORDS_KEY`` 同一处置。
    """
    if EXT_SPACES not in options:
        return None
    raw = options.pop(EXT_SPACES)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ValidationError(f"{EXT_SPACES} 必须是 list[str]")
    if not all(isinstance(item, str) for item in raw):
        raise ValidationError(f"{EXT_SPACES} 的每一项必须是 str")
    return list(raw)


def _space_denied(space: str, error_type: str, message: str) -> ChannelError:
    """把一个被判权剔除的候选空间记成结果对象上的结构化错误。

    构造委托 :func:`~retrieval.cross_space.space_error`：判权剔除在本层、扇出失败在
    :mod:`~control.collective.cross_space_recall`，两处产出同一形态的错误项，各写一份
    即 ``channel`` / ``source`` 的编码在两层各有一份。
    """
    return space_error(space, error_type, message)


def _take_coords(
    metadata: dict[str, MetadataValueType] | None,
    *,
    enabled: bool,
) -> tuple[dict[str, str] | None, dict[str, MetadataValueType] | None]:
    """从写入侧参数袋取出归属坐标，返回 ``(坐标, 去掉该键的参数袋)``。

    **``enabled`` 为假即整体不介入。** 未装配判定表的部署里坐标不产生落点，取出它等于把
    调用方传的键静默吞掉——条目照常写入，坐标既不落盘也不报错。留在袋里则照旧由
    :func:`_reject_non_scalar_metadata` 按嵌套字典拒绝，与本特性之前逐字一致。

    **取出而不是留在袋里。** :func:`_reject_non_scalar_metadata` 只接受 JSON 标量与
    字符串数组，坐标是嵌套字典，留在袋里会被它拒绝。``ROUTE_CTX_KEY`` 不受此限是因为
    它由内核在该校验之后写入，而坐标是调用方给的那一份，先于校验到达。

    **不就地修改入参。** 参数袋对象归调用方所有，写入侧不产生调用方可见的副作用。
    """
    if not enabled or not metadata or COORDS_KEY not in metadata:
        return None, metadata
    rest = {key: value for key, value in metadata.items() if key != COORDS_KEY}
    return _parse_coords(metadata[COORDS_KEY]), (rest or None)


def _reject_non_scalar_metadata(
    metadata: dict[str, MetadataValueType] | None, *, field_name: str
) -> None:
    """写入/更新边界只接受 JSON 标量与字符串数组。

    嵌套 dict/混合类型数组在各后端语义不一：ES 会把嵌套对象展开成 ``metadata.a.b``
    另建 mapping，Milvus 的 JSON 字段能存但过滤算子对其未定义，UnitReader 又会把
    list 当集合字段走成员语义。三方各行其是且无一致契约，因此在入口挡住。
    字符串数组是例外——tags 类用法有明确的成员包含语义（``json_contains`` / ``term``）。
    """
    if not metadata:
        return
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(f"{field_name} key 必须是非空字符串")
        if isinstance(value, float) and not isfinite(value):
            raise ValidationError(f"{field_name}[{key!r}] 的浮点数必须有限")
        if isinstance(value, (str, int, float, bool)) or value is None:
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            continue
        raise ValidationError(
            f"{field_name}[{key!r}] 仅支持 JSON 标量或字符串数组，"
            f"收到 {type(value).__name__}"
        )


def _with_author_marks(metadata: dict[str, Any] | None, identity: Scope) -> dict[str, Any]:
    """按调用方身份写入作者标记（F07）。

    落 API 层而非引擎层：``identity`` 不下沉，下游只信任已鉴权的 target scope。
    两个键恒写入、取值为空也不省略——检索侧要用「取值为空串」作过滤条件，键缺失会使该
    条件的结果依后端实现而定。

    调用点在保留键校验之后：先拒绝调用方占用这些键，再由内核写入，两者不冲突。
    """
    author_principal, author_agent = principal.derive_author(identity)
    return {
        **(metadata or {}),
        principal.AUTHOR_PRINCIPAL: author_principal,
        principal.AUTHOR_AGENT: author_agent,  # 空串也写入，不省略键
    }


def _policy_bool(policy: PolicyManager, key: str, *, default: bool = False) -> bool:
    try:
        raw = policy.get(key)
    except PolicyError:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _policy_int(policy: PolicyManager, key: str, *, default: int) -> int:
    """读正整数策略项；缺失、非整数或非正一律回落缺省值并记一条 WARNING。

    配置项写错时静默按缺省值跑，好过整个部署起不来——本项是规模上限而非判据。
    """
    try:
        raw = policy.get(key)
    except PolicyError:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(
            "policy %s is not an integer: value=%s, falling back to %d",
            key,
            metadata_for_log({"value": raw}),
            default,
        )
        return default
    if value <= 0:
        logger.warning(
            "policy %s must be > 0, value=%s, falling back to %d",
            key,
            metadata_for_log({"value": value}),
            default,
        )
        return default
    return value


def _missing_required_space(
    policy: PolicyManager,
    target: Scope,
    require_space: bool,
) -> bool:
    if not require_space:
        return False
    if target == _ROOT:
        return False
    if target.space:
        return False
    return _policy_bool(policy, "scope.require_space")


def _write_permission_context(
    scope: Scope,
    tags: list[str] | None,
    system_metadata: dict[str, MetadataValueType] | None,
) -> PermissionContext:
    """构造写入鉴权上下文。

    瞬态键不进上下文：``infer`` / ``procedural`` 的写入路径经 ``route_ctx`` 把
    :class:`RouteContext` 对象放进 ``system_metadata`` 交给构建层，它不是判据、也不可
    序列化，``str()`` 后是上千字符的对象字面量。与 :func:`_recall_permission_context`
    跳过 ``coords`` / ``spaces`` 同向——编排用的键不越过持久化与判定边界。
    """
    meta = {
        key: str(value)
        for key, value in (system_metadata or {}).items()
        if key not in TRANSIENT_SYSTEM_METADATA_KEYS
    }
    return PermissionContext(
        resource_type="write_input",
        memory_type=meta.get("memory_type", "").strip(),
        pipeline=meta.get("pipeline", "").strip(),
        scope=scope,
        tags=tuple(tags or ()),
        metadata=meta,
    )


def _recall_permission_context(
    context: Context,
    filters: FilterExpr | None,
) -> PermissionContext:
    """构造查询鉴权上下文：按 **S03 的路由取值规则**解析，授权针对真正会执行的 profile。

    S03「查询侧优先读取 ``extensions[route_key]``，其次读等值 filter」是路由取值的
    唯一口径。权限若另立一套（只认 filters），就会出现「按 A 授权、按 B 执行」——
    调用方用 ``filters=general`` + ``extensions=coding`` 即可拿宽松策略的授权去跑
    coding profile。这里与 S03 同源解析，两侧必然指向同一个 delegate。

    含义是 extensions 能决定命中哪条 policy，因此各 profile 的 policy 必须与该
    profile 可触达的数据相匹配——这是部署配置的责任，路由层不做补偿（S03「routing
    不改变授权语义，只选择 delegate」）。
    """
    routed_metadata = _required_filter_metadata(filters)
    # S03「MemoryPipeline 路由规则」——extensions 优先。
    for key, override in context.extensions.items():
        # 归属坐标与候选空间列表不进权限上下文：前者是判定输入、后者是编排开关，都不是
        # 路由取值。留下会被下面 str 化成 "{'project': 'p-alpha'}" / "['p_apollo']" 这样
        # 的字符串，既污染鉴权入参，又可能与某条 policy 的路由字段撞上。
        if key in (COORDS_KEY, EXT_SPACES):
            continue
        effective = str(override).strip()
        if effective:
            routed_metadata[key] = effective
    return PermissionContext(
        resource_type="query",
        memory_type=routed_metadata.get("memory_type", ""),
        pipeline=routed_metadata.get("pipeline", ""),
        scope=context.scope,
        metadata=routed_metadata,
    )


def _list_permission_contexts(
    scope: Scope,
    memory_types: list[str] | None,
    filters: FilterExpr | None,
    extensions: dict[str, Any],
) -> list[PermissionContext]:
    routed_metadata = _required_filter_metadata(filters)
    for key, override in extensions.items():
        effective = str(override).strip()
        if effective:
            routed_metadata[key] = effective
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_memory_type in memory_types or []:
        memory_type = str(raw_memory_type).strip()
        if not memory_type or memory_type in seen:
            continue
        cleaned.append(memory_type)
        seen.add(memory_type)
    if not cleaned:
        return [
            PermissionContext(
                resource_type="memory_list",
                memory_type=routed_metadata.get("memory_type", ""),
                pipeline=routed_metadata.get("pipeline", ""),
                scope=scope,
                metadata=dict(routed_metadata),
            )
        ]
    joined = ",".join(cleaned)
    contexts: list[PermissionContext] = []
    for memory_type in cleaned:
        contexts.append(
            PermissionContext(
                resource_type="memory_list",
                memory_type=memory_type,
                pipeline=routed_metadata.get("pipeline", ""),
                scope=scope,
                metadata={**routed_metadata, "memory_types": joined},
            )
        )
    return contexts


def _normalize_list_extensions(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("extensions must be a dict")
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValidationError("extensions keys must be strings")
        normalized[key] = str(value)
    return normalized


def _permission_route_value(context: PermissionContext, field: str) -> str:
    if field == "memory_type":
        return context.memory_type
    if field == "pipeline":
        return context.pipeline
    if field == "resource_type":
        return context.resource_type
    return str(context.metadata.get(field, "")).strip()


def _list_routing_clauses(
    contexts: list[PermissionContext],
    routing_fields: tuple[str, ...],
    memory_types: list[str] | None,
) -> list[FilterClause]:
    has_memory_types = any(str(value).strip() for value in memory_types or ())
    clauses: list[FilterClause] = []
    for field in routing_fields:
        if field == "memory_type" and has_memory_types:
            continue
        values: set[str] = set()
        for context in contexts:
            value = _permission_route_value(context, field)
            if value:
                values.add(value)
        if len(values) == 1:
            clauses.append(
                FilterClause(canonical_filter_field(field), FilterOp.EQ, values.pop())
            )
    return clauses


def _routing_clauses_of(
    context: PermissionContext, routing_fields: tuple[str, ...]
) -> list[FilterClause]:
    """把**授权所依据的路由值**回注为系统谓词。

    按 ``memory_type=notes`` 授的权，这次查询就只能读到 ``memory_type=notes`` 的数据。否则
    「选哪条策略」与「能读到哪些数据」是两个独立输入、且都由调用方控制——路由值填宽松策略
    对应的类型、``filters`` 指向受严格策略保护的数据，即可用 A 的钥匙开 B 的门。

    单空间与跨空间两条路径共用本函数：只挂一条时未挂的那条即该绑定的绕过通道，而跨空间
    路径在显式给出候选空间时不依赖反查索引，始终可达。
    """
    clauses: list[FilterClause] = []
    for field in routing_fields:
        routed = context.metadata.get(field, "").strip()
        if routed:
            clauses.append(FilterClause(canonical_filter_field(field), FilterOp.EQ, routed))
    return clauses


def _selector_permission_context(selector: DeleteSelector, scope: Scope) -> PermissionContext:
    return PermissionContext(
        resource_type="delete_selector",
        scope=scope,
        tags=tuple(selector.tags),
        metadata={"mode": selector.mode.value},
    )


def _unit_lookup_permission_context(unit_id: str, scope: Scope) -> PermissionContext:
    return PermissionContext(
        resource_type="memory_unit_lookup",
        unit_id=unit_id,
        scope=scope,
    )


def _space_scope(org: str, space: str) -> Scope:
    return Scope(org=org, space=space)


def _space_target_id(org: str, space: str) -> str:
    return f"{org}/{space}"


def _space_permission_context(resource_type: str, scope: Scope) -> PermissionContext:
    return PermissionContext(resource_type=resource_type, scope=scope)


def _first_family_predicate(actor: Scope, context: PermissionContext | None) -> list[FilterClause]:
    """检索谓词第一族：个体空间的系统谓词（F07「检索两族谓词」）。

    实现在 :func:`~common.security.space_predicates.individual_space_predicates`，本函数
    只从权限上下文里取出空间事实。两族谓词的生成规则集中在安全层的一个模块，因为跨空间
    检索侧要用同一份规则——
    两处各写一份即出现「单空间搜不到、跨空间搜得到」。
    """
    facts = context.space_facts if context is not None else None
    return space_predicates.individual_space_predicates(facts, actor)


def _is_space_level_entry(entry: str) -> bool:
    """该入口是否落空间级判定。

    未登记的入口与组织级入口都不是：前者尚未纳入空间级判定，后者由管理面角色闸门
    终局裁决。组织级角色不属本特性范围，两者一律回落改造前的判据，形态校验也随之不执行。
    """
    rule = ENTRY_RULES.get(entry)
    return rule is not None and rule.axis is not SpaceAxis.ORG


def _trim_space_policy(info: SpaceInfo, axis: str) -> SpaceInfo:
    """按通过的轴裁剪空间策略（F07「空间策略必须从元数据返回值中裁剪」）。

    经治理轴通过可读策略，只经内容轴通过则策略置空。裁剪不另发起一次判定，轴取自本次
    鉴权已经带出的结论。

    轴为空表示本次判定无轴概念（未装配空间级判定，或入口回落改造前的判据），不裁剪，
    行为与改造前一致。

    ``principal_path`` 在 :class:`SpaceInfo` 上有两份镜像（顶层字段与策略内字段），空间
    管理器在建空间、改空间与设策略三处都同步写两份。裁剪保留策略内那份：置空则两份镜像
    相互矛盾，而顶层字段并未被裁剪，读它照样得到真值——遮不住却先自相矛盾。

    其余字段置为默认值形态。默认值不表示「不可见」：调用方无从区分「该空间的策略确为
    默认值」与「策略被裁剪」，要读真值须改调 ``get_space_policy``（治理轴）。
    """
    if axis in ("", SpaceAxis.GOVERNANCE.value):
        return info
    return replace(info, policy=SpacePolicy(principal_path=info.principal_path))


def _evolve_space_action(mode: EvolveMode | str) -> SpaceAction:
    """演进模式对应的判定动作（F07「入口到轴与动作的映射」）。

    去重（``CONSOLIDATE``）与遗忘（``FORGET``）改写既有条目，取 ``UPDATE``；其余模式
    产出新条目，取 ``WRITE``。两者都不放宽到「本人所写」——输入是一批条目，逐条判会使
    一次调用部分生效部分被拒。

    任务入口按发起该作业的模式取同一动作，因此与 ``evolve`` 共用本函数。取值无法解析
    时取 ``WRITE``：存量任务记录的 ``mode`` 是自由字符串，解析失败按默认动作处置而不是
    拒绝整个查询。
    """
    try:
        resolved = EvolveMode(mode)
    except ValueError:
        return SpaceAction.WRITE
    if resolved in (EvolveMode.CONSOLIDATE, EvolveMode.FORGET):
        return SpaceAction.UPDATE
    return SpaceAction.WRITE


def _is_status_only(patch: SpacePatch | None) -> bool:
    """该次修改是否只改生命周期状态。

    ``None`` 取假：状态校验的调用方未传 patch 时无从判断改了什么，冻结态按拒绝处置。
    """
    if patch is None or patch.status is None:
        return False
    return all(
        getattr(patch, field.name) is None
        for field in fields(patch)
        if field.name != "status"
    )


def _project_space_facts(facts: SpaceFacts) -> SpaceAuthorizationFacts:
    """控制层的空间事实折算为判定用的最小投影（F07「空间事实的两层投影」）。

    一次纯计算、不访问存储。空间策略、生命周期状态与成员记录的时间戳不进投影——
    判定不看它们，状态校验由鉴权点在授权通过之后另行执行。

    成员记录的存量单轴角色已在空间管理器读盘时解析成两轴，本函数直取两个字段。
    """
    owners = tuple(facts.info.owners) if facts.info is not None else ()
    members = tuple(
        SpaceMemberFact(
            scope=member.scope,
            content_role=member.content_role,
            governance_role=member.governance_role,
        )
        for member in facts.members
    )
    return SpaceAuthorizationFacts(owners=owners, members=members)

__all__ = [
    "_ROOT",
    "_SPACE_SCAN_CAP",
    "_GRANT_ACTION_BACK",
    "_LEGACY_PERMISSION_ACTIONS",
    "_validate_legacy_permission_actions",
    "_parse_max_tokens",
    "_context_detail",
    "_required_filter_metadata",
    "_reject_invalid_content",
    "_resolve_space_owner",
    "_reject_kernel_system_metadata",
    "_truthy_metadata",
    "_reject_route_tag_keys",
    "_reject_foreign_routed_scope",
    "_reject_foreign_write_scope",
    "_space_level_scope",
    "_parse_coords",
    "_pop_coords",
    "_pop_spaces",
    "_space_denied",
    "_take_coords",
    "_reject_non_scalar_metadata",
    "_with_author_marks",
    "_policy_bool",
    "_policy_int",
    "_missing_required_space",
    "_write_permission_context",
    "_recall_permission_context",
    "_list_permission_contexts",
    "_normalize_list_extensions",
    "_permission_route_value",
    "_list_routing_clauses",
    "_routing_clauses_of",
    "_selector_permission_context",
    "_unit_lookup_permission_context",
    "_space_scope",
    "_space_target_id",
    "_space_permission_context",
    "_first_family_predicate",
    "_is_space_level_entry",
    "_trim_space_policy",
    "_evolve_space_action",
    "_is_status_only",
    "_project_space_facts",
]
