# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Data-plane write: add/batch and write-target resolution."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from jiuwen_memory.common.errors import (
    PermissionDeniedError,
    PolicyError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    ROUTE_CTX_KEY,
    MemoryUnit,
    MetadataValueType,
    Modality,
    Scope,
)
from jiuwen_memory.construction.router import (
    RouteContext,
    RouteDecision,
    degraded_reasons,
    fill_missing_tag_keys,
    reject_kernel_coords,
)
from jiuwen_memory.control import collective
from jiuwen_memory.control.types import (
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
)

from .local_support import (
    _reject_foreign_routed_scope,
    _reject_foreign_write_scope,
    _reject_invalid_content,
    _reject_kernel_system_metadata,
    _reject_non_scalar_metadata,
    _reject_route_tag_keys,
    _space_level_scope,
    _strip_transient_metadata,
    _take_coords,
    _truthy_metadata,
    _with_author_marks,
    _write_permission_context,
)

logger = get_logger("jiuwen_memory.api.memory_api_impl.local_memory_api")


class WriteOpsMixin:
    """Data-plane write: add/batch and write-target resolution."""

    def add(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        return asyncio.run(
            self.add_async(
                content,
                scope,
                source,
                security=security,
                assets=assets,
                tags=tags,
                system_metadata=system_metadata,
                user_metadata=user_metadata,
                occurred_at=occurred_at,
            )
        )

    async def add_async(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        _reject_invalid_content(content)
        # 三项校验都在落点解析之前：解析会往 system_metadata 塞判定产物与瞬态的
        # route_ctx（非标量），先校验才是校调用方给的那份。
        # 坐标先取出：它是嵌套字典，留在参数袋里会被下面的标量校验拒绝。
        coords, system_metadata = _take_coords(
            system_metadata, enabled=self._routing_enabled()
        )
        _reject_kernel_system_metadata(system_metadata)
        _reject_route_tag_keys(system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(user_metadata, field_name="user_metadata")
        reject_kernel_coords(coords)
        target, system_metadata = self._write_target(
            scope, identity, coords, content, system_metadata
        )
        permission_context = _write_permission_context(target, tags, system_metadata)
        auth = self._authorize(
            identity,
            target,
            Action.WRITE,
            "add",
            context=permission_context,
        )
        self._ensure_space_writable(target)
        units = await self._commands.write(
            content,
            target,
            source,
            assets=assets,
            tags=tags,
            system_metadata=_with_author_marks(system_metadata, identity),
            user_metadata=user_metadata,
            occurred_at=occurred_at,
        )
        self._log(identity, "add", target_scope=target, detail=auth)
        return [_strip_transient_metadata(unit) for unit in units]

    def _routes_by_decision(
        self,
        scope: Scope | None,
        coords: Mapping[str, str] | None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """本次写入是否交给归属判定：由参数袋里有没有 ``coords`` 键决定。

        判据取键的有无，不取 ``scope`` 的取值形态。「判定表非空」即
        :meth:`_routing_enabled`，取值来自配置的 ``router`` 命名空间：

        | 判定表非空 | ``coords`` 键 | ``scope.space`` | 处置 |
        |---|---|---|---|
        | 否 | 任意 | 任意 | 直写 |
        | 是 | 有 | 任意 | 走判定，``scope.space`` 不参与落点 |
        | 是 | 无 | 非空 | 直写 |
        | 是 | 无 | 空 | 拒绝：既未指定落点，也未请求判定 |

        **``middle=true`` 不判定**（F07「归属判定的生效范围」），按无 ``coords`` 键的三行
        处置。该路径把原文按 ``tier=WORKING`` 直接建索引（见
        :meth:`~control.engine_impl.in_memory_engine.InMemoryEngine._write_middle_path`），
        与 ``infer`` / ``procedural`` 的载体不同——后两者的原文进 ``message_store``、不参与
        检索。走判定分流则它落 fallback 且一个收窄维标签键都不落，随后在任何带 ``coords``
        的检索里被第二族谓词 ``IN ["", value]`` 静默排除，正是不变量 8 要避开的形态。

        **判据是调用方的一个动作，不是某个字段的缺省状态。** ``space`` 为空是调用方什么都
        没表达时的取值，拿它触发另一条处理路径等于由内核替调用方解读缺省值。

        **``space`` 非空不作反向判据。** 上游网关按自己的租户或应用标识填 ``space`` 是常见
        形态，那个取值不是本系统的空间标识。以它否决判定请求，这类接入方将无路可走——直写要求
        该空间已在本系统登记，未登记即判权拒绝，且内核不为调用方给的空间名自动创建（见
        :meth:`_ensure_fallback_space`）。判定请求因而优先；交出落点决定权是 ``coords`` 键
        的既定语义，真实落点又由返回的记忆单元携带。

        **键的有无与值的内容分开。** ``coords`` 为 ``{}`` 是合法的判定请求，表示「请判定，
        但本次没有业务坐标」——内核坐标由 ``kernel_coords`` 从身份填入。取值判空则这层意图
        只能退回缺省状态触发，正是本判据要避开的形态。

        **末行的拒绝替换的是原本的判权拒绝**：启用判定的部署里空 ``space`` 拿不到空间事实，
        报出 ``permission denied``，与真正的越权不可区分。未装配判定算子的部署不受影响——
        ``space`` 为空是那里的既有合法落点（``InMemoryEngine`` 要求 ``space`` 为空串），
        该拒绝不成立。
        """
        if not self._routing_enabled():
            return False
        if scope is not None and not isinstance(scope, Scope):
            # 类型校验须先于 scope.space，否则非 Scope 入参在这里得到的是 AttributeError
            # 而不是 ValidationError；直写路径的同名校验在本判据之后，兜不到这条。
            raise ValidationError("scope must be Scope")
        if coords is not None and not _truthy_metadata(metadata, "middle"):
            return True
        if scope is None or not scope.space:
            raise ValidationError(
                "写入落点未声明：给 scope.space 指定落点，"
                f"或给 system_metadata[{COORDS_KEY!r}] 交由归属判定"
            )
        if coords is not None:
            # 坐标本次不生效，记 WARNING 而不静默丢弃：「以为坐标生效了、其实没有」从
            # 调用侧看不出差别。排在落点校验之后——落点未声明时抛出的异常已是终局信号，
            # 再记一条告警只是噪声。
            logger.warning(
                "write: middle=true 不参与归属判定，本次 %r 不生效，落点取 scope.space",
                COORDS_KEY,
            )
        return False

    def _write_target(
        self,
        scope: Scope,
        identity: Scope,
        coords: dict[str, str] | None,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> tuple[Scope, dict[str, Any] | None]:
        """定妥本次写入的落点与要补写的判定产物。

        落点有两个来源，不叠加：请求判定即以判定结果为落点，``scope.space`` 不再参与。
        分流判据见 :meth:`_routes_by_decision`；走到本方法体内的只有三种形态：

        | 调用形态 | 落点 | 判定 |
        |---|---|---|
        | 给 ``space`` | 就是它（启用判定时归一为空间级两维） | 否 |
        | 给 ``coords`` + ``infer``/``procedural`` 为真 | fallback 空间作载体 | 在构建层 |
        | 给 ``coords`` + 其余情形 | 判定算子在候选集内选 | 在本层，整条内容作一条候选送判 |

        ``middle=true`` 不在表内：它在分流判据处即被排除，走第一行（见
        :meth:`_routes_by_decision`）。

        原文与消息维护落 fallback 空间，判定只改派生单元的 scope，引擎写入签名不变。
        """
        if not self._routes_by_decision(scope, coords, metadata):
            return self._explicit_scope_target(scope, identity, metadata)
        _reject_foreign_routed_scope(scope, identity)
        outcome = self._routed_targets([(0, content, metadata)], identity, coords)[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _explicit_scope_target(
        self, scope: Scope | None, identity: Scope, metadata: dict[str, Any] | None
    ) -> tuple[Scope, dict[str, Any] | None]:
        """调用方传了 ``scope`` 的那条路径：不判定，只做归一与两项补齐。"""
        if scope is None:
            # 只有批量入口到得了这里：两级 scope 都缺省，批级参数袋又没请求判定。
            # 措辞与改造前一致——该形态在改造前后都是同一种调用错误。
            raise ValidationError("batch item scope is required")
        if not isinstance(scope, Scope):
            raise ValidationError("scope must be Scope")
        if not self._routing_enabled():
            return scope, metadata
        # 主体维校验与落盘 scope 归一同时生效，两者是同一件事的两半：归一把主体维从
        # 落盘键上去掉，校验保证去掉的那部分与调用方身份一致而不是被静默丢弃。未启用
        # 判定的部署两者都不做——落盘 scope 语义未变，跨主体写入照旧由判定链拒绝，
        # 拒绝类型也照旧是 PermissionDeniedError。
        _reject_foreign_write_scope(scope, identity)
        # 判定标签键在这条路径上同样补齐：不变量的定义域是落盘条目而非判定产物。
        # 不补则同一空间内两条写入路径产出的条目在带 coords 的检索里表现不同——
        # 判定路径写的能召回，这条路径写的被静默漏掉，且调用方不会收到任何提示。
        # 取值一律空串：本路径不经判定，「这条属于哪个项目」无从得知，而空串的语义
        # 是「不特定于任何坐标，因此对任何坐标都可见」，与判为否的条目同一待遇。
        return _space_level_scope(scope), fill_missing_tag_keys(
            metadata, self._route_table.tag_keys
        )

    def _routed_targets(
        self,
        entries: Sequence[tuple[Any, str, dict[str, Any] | None]],
        identity: Scope,
        coords: dict[str, str] | None,
    ) -> dict[Any, tuple[Scope, dict[str, Any] | None] | Exception]:
        """交由归属判定的写入：整批一次判定，逐项产出落点与要补写的判定产物。

        入参每项是 ``(key, content, metadata)``，返回按 key 索引；判定失败的项返回异常对象
        而不抛出，由调用处按各自的 ``continue_on_error`` 语义处置——批量入口里一项失败不该
        使整批中止。

        **判定上下文每批只算一次。** ``coords`` 与 ``identity`` 批内恒定，候选空间集合与
        逐空间判权因而是同一份；逐项各算一次即把判权次数乘以批大小。判定本身也整批一次送判，
        理由见 :func:`~control.collective.routing.route_many`。
        """
        if not entries:
            return {}
        if not self._routing_enabled():
            # 防御性分支：两个调用点都经 _routes_by_decision 前置，未装配判定表时不会走到
            # 这里。留着是因为本方法的正确性不该依赖调用点记得先判——真到了这里，说明
            # 分流判据与本方法的前提脱了钩，报错比按空判定表继续算下去可诊断。
            error = ValidationError(
                f"system_metadata[{COORDS_KEY!r}] is not a routing request here: "
                "no routing table is configured, the decision path is unreachable"
            )
            return {key: error for key, _content, _metadata in entries}
        try:
            ctx = self._route_context(identity, coords)
        except Exception as exc:  # noqa: BLE001 —— 上下文构造失败逐项回传，不中止整批
            return {key: exc for key, _content, _metadata in entries}

        resolved: dict[Any, tuple[Scope, dict[str, Any] | None] | Exception] = {}
        pending: list[tuple[Any, str, dict[str, Any]]] = []
        for key, content, metadata in entries:
            merged = dict(metadata or {})
            if _truthy_metadata(merged, "infer") or _truthy_metadata(merged, "procedural"):
                # 派生路径：判定在构建层逐条进行，本层只把上下文经瞬态键传下去。载体（原文与
                # 消息维护）落 fallback 空间——它是本次候选集内最窄的那个。
                #
                # **为什么走 system_metadata 而不是 extensions。** F04「运行时对象不塞进
                # metadata」要求这类调用级依赖统一经 ``Context.extensions`` 透传，但
                # ``add`` / ``batch_add`` 的签名里没有 ``Context`` 参数——``extensions``
                # 是检索侧的通道，写入路径上不存在，到不了引擎内部的抽取链路。本层与构建层
                # 之间唯一贯通的容器就是 ``system_metadata``。
                #
                # 因此它按瞬态键处置，三条约束把「不进持久元数据语义」这层意思补回来：
                # 编解码器序列化时剥除、不落盘（``TRANSIENT_SYSTEM_METADATA_KEYS``）；
                # 不进权限上下文（见 ``_write_permission_context``）；``MetadataValueType``
                # 不因它放宽，只在写入边界的标量校验里对该键单独放行。
                merged[ROUTE_CTX_KEY] = ctx
                resolved[key] = (ctx.fallback, merged)
                continue
            pending.append((key, content, merged))
        if pending:
            decisions = collective.route_many(
                self._router, [content for _key, content, _meta in pending], ctx
            )
            for (key, _content, merged), decision in zip(pending, decisions):
                merged.update(collective.decision_metadata(decision))
                if coords:
                    # coords 在入口被 _take_coords 取出，判定产物只回写标签与类别，
                    # 不塞回则 md 落盘（_md_path 读 coords.project）与影子索引
                    # （_project_of）都读不到坐标，project_memory 全落 memory/default/。
                    # 与派生路径同处置：OrchestratingEvolver._route 从 ctx.coords 回写。
                    # coords 是瞬态键，dumps 进 unit_json 时剥除，但 md.write 与
                    # shadow.insert_units 在序列化之前从 unit 对象读，路径计算不受影响。
                    merged[COORDS_KEY] = dict(coords)
                resolved[key] = (decision.scope, merged)
            self._log_routing_degradation(identity, ctx, decisions)
        return resolved

    def _log_routing_degradation(
        self, identity: Scope, ctx: RouteContext, decisions: Sequence[RouteDecision]
    ) -> None:
        """把本批里没按判定原样落点的条数与逐原因计数记进审计。

        判定降级不阻断写入，因此在调用方看来它与「判定就是这么判的」完全一样：落点是
        fallback、内容照常落盘、不抛异常。``RouteDecision.reason`` 已经把原因区分开，但
        它止于本层——``decision_metadata`` 只回写判定标签与类别记录键，不带 reason，落盘
        条目上因此看不出本条是判出来的还是回落来的。

        **本路径落审计而非日志，判据是记录的消费者**（F07「降级记录按消费者分通道」）：
        结论直写的落点是调用方动作的直接结果，须可按 actor / target 检索并逐次回放。派生
        单元的判定在构建层，消费者是运维告警，落 ``WARNING`` 日志——见
        ``OrchestratingEvolver._route``。两条路径共用同一套 ``reason`` 词汇。

        **逐原因计数不可省。** 四类降级的处置完全不同：未装配是部署形态、判定器故障要查
        插件、条数不符是实现违约、落点越界是判定试图扩权。只报总数时说得出「有降级」，
        说不出该找谁。

        按原因聚合记一条，不逐条记：``coords`` 与候选集批内恒定，同一批的降级原因通常同源，
        逐条记只是把同一句话重复 N 遍。原样落点的条目不记——那是正常路径。
        """
        counted = degraded_reasons(decisions)
        if not counted:
            return
        self._log(
            identity,
            "add",
            ctx.fallback.space,
            target_scope=ctx.fallback,
            detail={
                "entry": "routing_degraded",
                "degraded": str(sum(counted.values())),
                "total": str(len(decisions)),
                "reasons": "; ".join(
                    f"{reason} x{count}" for reason, count in counted.most_common()
                ),
            },
        )

    def _batch_write_targets(
        self,
        entries: Sequence[tuple[int, BatchWriteItem]],
        identity: Scope,
        coords: dict[str, str] | None,
    ) -> dict[int, tuple[Scope, dict[str, Any] | None] | Exception]:
        """批量入口的落点解析：显式 ``scope`` 的项逐项处理，其余整批一次判定。"""
        resolved: dict[int, tuple[Scope, dict[str, Any] | None] | Exception] = {}
        routed: list[tuple[int, str, dict[str, Any] | None]] = []
        for index, item in entries:
            # 分流判据本身会拒绝两种落点声明形态（见 _routes_by_decision），拒绝逐项回传
            # 而不中止整批，与其余两类失败同一处置。coords 是批级的，逐项判据只差 scope。
            try:
                routes = self._routes_by_decision(item.scope, coords, item.system_metadata)
                if routes:
                    _reject_foreign_routed_scope(item.scope or Scope(), identity)
                else:
                    resolved[index] = self._explicit_scope_target(
                        item.scope, identity, item.system_metadata
                    )
            except Exception as exc:  # noqa: BLE001 —— 逐项回传，与判定路径同一处置
                resolved[index] = exc
                continue
            if routes:
                routed.append((index, item.content, item.system_metadata))
        resolved.update(self._routed_targets(routed, identity, coords))
        return resolved

    @staticmethod
    def _batch_error_item(item: object) -> BatchWriteItem:
        if isinstance(item, BatchWriteItem):
            return item
        return BatchWriteItem(content="")

    @staticmethod
    def _merge_batch_tags(
        defaults: list[str] | None, item_tags: list[str] | None
    ) -> list[str] | None:
        if defaults is None and item_tags is None:
            return None
        merged: list[str] = []
        for tag in [*(defaults or []), *(item_tags or [])]:
            if tag not in merged:
                merged.append(tag)
        return merged

    @staticmethod
    def _batch_outcome(
        index: int, item: object, error: Exception, *, error_type: str | None = None
    ) -> BatchWriteOutcome:
        return BatchWriteOutcome(
            index=index,
            item=WriteOpsMixin._batch_error_item(item),
            error=str(error),
            error_type=error_type or type(error).__name__,
        )

    def _normalize_batch_item(
        self,
        item: object,
        *,
        scope: Scope | None,
        source: Modality,
        tags: list[str] | None,
        system_metadata: dict[str, MetadataValueType] | None,
        user_metadata: dict[str, MetadataValueType] | None,
        occurred_at: datetime | None,
        stream_id: str,
    ) -> BatchWriteItem:
        """归一一项批量写入；逐项 ``scope`` 为 ``None`` 即沿用批级取值。

        两级都不给时归一结果为 ``None``，本处放行：判定请求是批级的，批级参数袋带 ``coords``
        时落点由判定给出，此时要求某一级给出 ``scope`` 是多余的。两级都不给又没请求判定的
        情形在调用处按 :meth:`_routes_by_decision` 分流时拒绝。
        """
        if not isinstance(item, BatchWriteItem):
            raise ValidationError("batch item must be BatchWriteItem")
        _reject_invalid_content(item.content)
        target_scope = item.scope if item.scope is not None else scope
        if target_scope is not None and not isinstance(target_scope, Scope):
            raise ValidationError("batch item scope must be Scope")
        item_source = item.source if item.source is not None else source
        if not isinstance(item_source, Modality):
            raise ValidationError("batch item source must be Modality")
        if item.assets is not None and (
            not isinstance(item.assets, list)
            or any(not isinstance(asset, str) for asset in item.assets)
        ):
            raise ValidationError("batch item assets must be list[str]")
        for values, name in ((tags, "tags"), (item.tags, "item tags")):
            if values is not None and (
                not isinstance(values, list) or any(not isinstance(value, str) for value in values)
            ):
                raise ValidationError(f"batch {name} must be list[str]")
        if item.system_metadata is not None and not isinstance(item.system_metadata, dict):
            raise ValidationError("batch item system_metadata must be dict")
        if item.user_metadata is not None and not isinstance(item.user_metadata, dict):
            raise ValidationError("batch item user_metadata must be dict")
        if item.occurred_at is not None and not isinstance(item.occurred_at, datetime):
            raise ValidationError("batch item occurred_at must be datetime")
        if item.system_metadata and COORDS_KEY in item.system_metadata:
            # 静默忽略的失效方向是「以为逐项坐标生效了、其实没有」，从调用侧看不出来。
            raise ValidationError(
                f"batch item 不得携带 {COORDS_KEY}：判定上下文每批只算一次，坐标取自批级参数袋"
            )
        merged_system_metadata = {
            **(system_metadata or {}),
            **(item.system_metadata or {}),
        }
        merged_user_metadata = {**(user_metadata or {}), **(item.user_metadata or {})}
        # 逐项校验而不是只校批级默认值：逐项的 system_metadata 同样是调用方入参。
        _reject_kernel_system_metadata(merged_system_metadata)
        _reject_route_tag_keys(merged_system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(merged_system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(merged_user_metadata, field_name="user_metadata")
        if item.stream_id and not isinstance(item.stream_id, str):
            raise ValidationError("batch item stream_id must be str")
        if item.sequence is not None and not isinstance(item.sequence, int):
            raise ValidationError("batch item sequence must be int")
        if not isinstance(item.idempotency_key, str):
            raise ValidationError("batch item idempotency_key must be str")
        return BatchWriteItem(
            content=item.content,
            scope=target_scope,
            source=item_source,
            assets=list(item.assets) if item.assets is not None else None,
            tags=self._merge_batch_tags(tags, item.tags),
            system_metadata=merged_system_metadata or None,
            user_metadata=merged_user_metadata or None,
            occurred_at=item.occurred_at if item.occurred_at is not None else occurred_at,
            stream_id=item.stream_id or stream_id,
            sequence=item.sequence,
            idempotency_key=item.idempotency_key,
        )

    def batch_add(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        return asyncio.run(
            self.batch_add_async(
                items,
                scope,
                source,
                security=security,
                tags=tags,
                system_metadata=system_metadata,
                user_metadata=user_metadata,
                occurred_at=occurred_at,
                stream_id=stream_id,
                continue_on_error=continue_on_error,
            )
        )

    async def batch_add_async(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        identity = security.auth.actor
        if not isinstance(items, list) or not items:
            raise ValidationError("batch items must be a non-empty list")
        if scope is not None and not isinstance(scope, Scope):
            raise ValidationError("batch scope must be Scope")
        if not isinstance(source, Modality):
            raise ValidationError("batch source must be Modality")
        if tags is not None and (
            not isinstance(tags, list) or any(not isinstance(value, str) for value in tags)
        ):
            raise ValidationError("batch tags must be list[str]")
        if system_metadata is not None and not isinstance(system_metadata, dict):
            raise ValidationError("batch system_metadata must be dict")
        if user_metadata is not None and not isinstance(user_metadata, dict):
            raise ValidationError("batch user_metadata must be dict")
        if not isinstance(stream_id, str):
            raise ValidationError("batch stream_id must be str")
        if occurred_at is not None and not isinstance(occurred_at, datetime):
            raise ValidationError("batch occurred_at must be datetime")
        # 坐标取自批级参数袋：判定上下文每批只算一次，逐项无处安放（逐项携带即拒绝，
        # 见 _normalize_batch_item）。同样须先于标量校验取出。
        coords, system_metadata = _take_coords(
            system_metadata, enabled=self._routing_enabled()
        )
        _reject_kernel_system_metadata(system_metadata)
        _reject_route_tag_keys(system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(user_metadata, field_name="user_metadata")
        reject_kernel_coords(coords)

        outcomes: dict[int, BatchWriteOutcome] = {}
        ready: list[tuple[int, BatchWriteItem]] = []
        seen_sequences: set[tuple[str, str, str, str, str, str, int]] = set()
        stopped_index: int | None = None

        def _record_failure(index: int, raw_item: object, exc: Exception) -> None:
            if not isinstance(exc, (ValidationError, PermissionDeniedError, PolicyError)):
                raise exc
            outcomes[index] = self._batch_outcome(index, raw_item, exc)
            error_scope = (
                raw_item.scope
                if isinstance(raw_item, BatchWriteItem) and isinstance(raw_item.scope, Scope)
                else scope
            )
            self._log(
                identity,
                "add",
                target_scope=error_scope,
                decision="error",
                detail={"error": str(exc), "error_type": type(exc).__name__},
            )

        # 第 1 遍：规范化。落点解析不在这一遍——space 为空的项要整批一次送判，逐项各判
        # 一次会使 Router「每批一次模型调用」的契约在批量入口整段失效（N 条即 N 次串行
        # 调用），候选空间集合的逐空间判权也跟着乘以批大小。
        normalized: list[tuple[int, BatchWriteItem]] = []
        for index, raw_item in enumerate(items):
            try:
                normalized.append(
                    (
                        index,
                        self._normalize_batch_item(
                            raw_item,
                            scope=scope,
                            source=source,
                            tags=tags,
                            system_metadata=system_metadata,
                            user_metadata=user_metadata,
                            occurred_at=occurred_at,
                            stream_id=stream_id,
                        ),
                    )
                )
            except Exception as exc:
                _record_failure(index, raw_item, exc)
                if not continue_on_error:
                    stopped_index = index
                    break

        # 第 2 遍：落点解析（整批一次）与 sequence 去重。两者的次序不可换：去重键含 scope
        # 五维，落点未定时算不出。判定按写入路径生效、不按单条与批量入口区分。
        targets = self._batch_write_targets(normalized, identity, coords)
        for index, item in normalized:
            try:
                outcome = targets.get(index)
                if isinstance(outcome, Exception):
                    raise outcome
                target, item_metadata = outcome
                item = replace(item, scope=target, system_metadata=item_metadata or None)
                if item.sequence is not None:
                    sequence_key = (
                        item.scope.org,
                        item.scope.space,
                        item.scope.user,
                        item.scope.agent,
                        item.scope.session,
                        item.stream_id,
                        item.sequence,
                    )
                    if sequence_key in seen_sequences:
                        raise ValidationError(
                            "duplicate sequence within the same scope and stream"
                        )
                    seen_sequences.add(sequence_key)
                ready.append((index, item))
            except Exception as exc:
                _record_failure(index, items[index], exc)
                if not continue_on_error:
                    stopped_index = index
                    break

        if stopped_index is not None:
            ready = [entry for entry in ready if entry[0] < stopped_index]
            for index in range(stopped_index + 1, len(items)):
                outcomes[index] = BatchWriteOutcome(
                    index=index,
                    item=self._batch_error_item(items[index]),
                    error="skipped after previous item failed",
                    error_type="Skipped",
                )

        authorized: list[tuple[int, BatchWriteItem, dict[str, str]]] = []
        for index, item in ready:
            permission_context = _write_permission_context(
                item.scope, item.tags, item.system_metadata
            )
            try:
                auth = self._authorize(
                    identity,
                    item.scope,
                    Action.WRITE,
                    "add",
                    context=permission_context,
                )
                self._ensure_space_writable(item.scope)
                authorized.append((index, item, auth))
            except (PermissionDeniedError, ValidationError, PolicyError) as exc:
                outcomes[index] = self._batch_outcome(index, item, exc)
                if not isinstance(exc, PermissionDeniedError):
                    self._log(
                        identity,
                        "add",
                        target_scope=item.scope,
                        decision="error",
                        detail={"error": str(exc), "error_type": type(exc).__name__},
                    )
                if not continue_on_error:
                    stopped_index = index
                    break

        if stopped_index is not None:
            for index in range(stopped_index + 1, len(items)):
                outcomes.setdefault(
                    index,
                    BatchWriteOutcome(
                        index=index,
                        item=self._batch_error_item(items[index]),
                        error="skipped after previous item failed",
                        error_type="Skipped",
                    ),
                )
            authorized = [entry for entry in authorized if entry[0] < stopped_index]

        if authorized:
            # 作者标记在鉴权与保留键校验之后写入，逐项各写一份（各项的 metadata 已归并）。
            # 只作用于交给引擎的那份：逐项结果回填的仍是调用方输入的归一化形态，内核标记
            # 是条目内容的一部分，不回显为「调用方传了这些键」。
            remapped = await self._commands.batch_write_aligned(
                [
                    replace(
                        item,
                        system_metadata=_with_author_marks(item.system_metadata, identity),
                    )
                    for _, item, _ in authorized
                ],
                [(index, item) for index, item, _ in authorized],
                continue_on_error=continue_on_error,
            )
            for engine_outcome, (index, item, auth) in zip(remapped, authorized):
                outcomes[index] = engine_outcome
                if engine_outcome.units:
                    # 批路径与 add 同处置：units 是回显给调用方的形态，瞬态键不越界。
                    engine_outcome.units = [
                        _strip_transient_metadata(unit) for unit in engine_outcome.units
                    ]
                self._log(
                    identity,
                    "add",
                    target_scope=item.scope,
                    decision="allow" if not engine_outcome.error else "error",
                    detail={
                        **auth,
                        "error": engine_outcome.error,
                        "error_type": engine_outcome.error_type,
                    },
                )

        return self._commands.collect_batch_result(outcomes, len(items))
