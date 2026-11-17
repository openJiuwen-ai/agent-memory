"""PEP and thin DTO adapters for the optional S10 application service."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    ConflictError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
    WriteIncompleteError,
)
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    ContinuityWrite,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    TaskRequest,
    fingerprint,
)
from jiuwen_memory.control.types import BatchWriteItem

from .local_support import (
    _strip_transient_metadata,
    _unit_snapshot_permission_context,
    _with_author_marks,
)


def _same_scope(left: Scope, right: Scope) -> bool:
    """Whether two items belong to the same project space (one record_batch per scope)."""
    return (
        left.org,
        left.space,
        left.user,
        left.agent,
        left.session,
    ) == (
        right.org,
        right.space,
        right.user,
        right.agent,
        right.session,
    )


class _Skipped:
    """Batch item skipped because an earlier item in the same run failed."""


_MANAGE_RELATION = ContextVar("continuity_manage_relation", default=False)
_READ_DEPTH = ContextVar("continuity_read_depth", default=0)


class ContinuityOpsMixin:
    def _source_available(self, scope, connector) -> bool:
        """A connector's source is available only if enabled AND not quarantined."""
        if not connector:
            return True
        if scope is None:
            # Merged results without a trusted per-item Scope cannot be checked against
            # scope-local quarantine state; never infer it from user metadata.
            return False
        enabled = self._continuity_connectors.get(connector, {}).get(
            "enabled", connector in self._continuity_connectors
        )
        if not enabled:
            return False
        if (
            self._continuity is not None
            and self._continuity.source_quarantine(scope, connector) is not None
        ):
            return False
        return True

    def _guard_continuity_snapshot(self, unit, security):
        """Recheck explicit dependencies on every public snapshot read, even if disabled."""
        metadata = unit.system_metadata
        connector = metadata.get("continuity_connector")
        if connector and not self._source_available(unit.scope, connector):
            raise PermissionDeniedError("source unavailable")
        refs = []
        try:
            if metadata.get("continuity_source_refs"):
                refs = [MemoryRef.parse(r) for r in json.loads(metadata["continuity_source_refs"])]
            elif metadata.get("continuity_envelope"):
                envelope = json.loads(str(metadata["continuity_envelope"]))
                refs = [MemoryRef.parse(r) for r in envelope.get("evidence_refs", [])]
        except (ValueError, KeyError, TypeError):
            raise PermissionDeniedError("unverifiable continuity record") from None
        if not refs:
            return
        depth = _READ_DEPTH.get()
        if depth >= 4:
            raise PermissionDeniedError("dependency verification budget exceeded")
        token = _READ_DEPTH.set(depth + 1)
        try:
            for ref in refs:
                self._role_reader(security, unit.scope)(ref)
        finally:
            _READ_DEPTH.reset(token)

    def _unavailable_continuity_connectors(self, scope) -> list[str]:
        """Connectors whose entries must not appear in ordinary reads (disabled ∪ quarantined)."""
        names = sorted(
            name
            for name, value in (self._continuity_connectors or {}).items()
            if not value.get("enabled", True)
        )
        if self._continuity is None:
            return names
        for name in (self._continuity_connectors or {}):
            if name in names:
                continue
            try:
                if self._continuity.source_quarantine(scope, name) is not None:
                    names.append(name)
            except Exception:
                # An unreadable quarantine state must not hide entries: stay conservative by
                # leaving the connector available and letting the read-time guard decide.
                continue
        return names

    @staticmethod
    def _requires_continuity_point_read(metadata) -> bool:
        if metadata.get("continuity_source_refs"):
            return True
        envelope = metadata.get("continuity_envelope")
        if not envelope:
            return False
        try:
            return bool(json.loads(str(envelope)).get("evidence_refs"))
        except (ValueError, TypeError):
            return True

    def _guard_continuity_search(self, result, scope=None, *, item_scopes=None):
        # Ordinary retrieval has no per-item real Scope/path contract. Structured records
        # with dependency-bearing payloads are disclosed only through guarded point reads.

        from jiuwen_memory.common.type_def import ChannelError, RecallChannel

        visible = []
        for item in result.items:
            metadata = item.system_metadata
            connector = metadata.get("continuity_connector")
            real_scope = item_scopes.get(id(item)) if item_scopes is not None else scope
            if connector and not self._source_available(real_scope, connector):
                continue
            if self._requires_continuity_point_read(metadata):
                continue
            visible.append(item)
        if len(visible) != len(result.items):
            result.items = visible
            result.errors.append(
                ChannelError(
                    RecallChannel.GRAPH,
                    "continuity",
                    "dependency_check_required",
                    "Use authorized references for dependency-bearing records",
                )
            )
        return result

    def _role_service(self, scope):
        has_project = bool(scope.org and scope.space)
        has_personal_context = bool(scope.user or scope.agent or scope.session)
        if not has_project or has_personal_context:
            raise ValidationError("continuity scope must be an org/project space")
        if self._continuity is None:
            raise UnsupportedCapabilityError("continuity is not configured")
        return self._continuity

    def _role_reader(self, security, project):
        def read(ref):
            if ref.scope.org != project.org:
                raise PermissionDeniedError("cross-organization reference")
            unit = self.get(ref.unit_id, ref.scope, security=security)
            connector = unit.system_metadata.get("continuity_connector")
            if connector and not self._source_available(ref.scope, connector):
                raise PermissionDeniedError("source unavailable")
            if MemoryRef.of(unit) != ref:
                raise ConflictError("reference", message="reference content changed")
            now = datetime.now(UTC)
            if unit.lifecycle.value != "active":
                raise ConflictError("reference", message="reference is not active")
            not_yet_valid = unit.temporal.t_valid and unit.temporal.t_valid > now
            expired = unit.temporal.t_invalid and unit.temporal.t_invalid <= now
            if not_yet_valid or expired:
                raise ConflictError("reference", message="reference is not currently valid")
            return unit

        return read

    def _role_management_reader(self, security, project):
        read = self._role_reader(security, project)

        def managed(ref):
            token = _MANAGE_RELATION.set(True)
            try:
                return read(ref)
            finally:
                _MANAGE_RELATION.reset(token)

        return managed

    def _role_writer(
        self,
        scope,
        security,
        *,
        event_time=None,
        tags=None,
        source_ref="",
        source=None,
        assets=None,
        system_metadata=None,
        user_metadata=None,
        occurred_at=None,
    ):
        def write(content, metadata):
            # 接续写入固定 infer/middle=false（原文直接落盘、不经分类器改写），
            # 调用方的其余系统键原样保留。
            # 只有**原始事件**继承事件时间；派生输出（metadata 带 continuity_output）是
            # 按构建时刻生成的新记忆，不能继承来源的发生时间（F10 4.2）。
            merged = {**(system_metadata or {}), "infer": "false", "middle": "false"}
            return asyncio.run(
                self._add_impl(
                    content,
                    scope,
                    source if source is not None else Modality.TEXT,
                    security=security,
                    assets=assets,
                    tags=tags,
                    system_metadata=merged,
                    user_metadata=user_metadata,
                    occurred_at=occurred_at,
                    continuity_metadata=metadata,
                    source_ref=source_ref,
                    # Source events carry their own time; derived outputs do not inherit it
                    # (F10 4.2).
                    event_time=(None if metadata.get("continuity_output") else event_time),
                )
            )

        return write

    def continuity_task(
        self,
        scope: Scope,
        request: TaskRequest,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        reading = request.operation in {"get", "find"}
        if reading:
            self._authorize(security, scope, Action.READ, "continuity_task_read")
        else:
            self._authorize(security, scope, Action.WRITE, "continuity_task_write")
        service = self._role_service(scope)
        owner = fingerprint(asdict(security.auth.actor))
        if request.operation == "create" and request.parent_task_id:
            self._authorize(security, scope, Action.READ, "continuity_task_parent")
        if request.operation not in {"create", "get", "find"}:
            if service.task_owner(scope, request.task_id) != owner:
                raise PermissionDeniedError("continuity_task_write")
        if request.operation == "bind":
            connector = self._continuity_connectors.get(request.connector_id)
            if not connector or connector.get("actor", {}).get("org") != scope.org:
                raise ValidationError("connector is not configured for this organization")
            if scope.space not in connector.get("projects", []):
                raise ValidationError("connector is not configured for this project")
        result = service.task(scope, request, owner, self._role_reader(security, scope))
        self._log(security.auth.actor, "continuity_task", request.task_id, target_scope=scope)
        return result

    def _require_relation_submitter(self, scope: Scope, security, connector: str) -> None:
        """Registration-side gate: the submitting connector, or project governance."""
        if connector and self._connector_for_actor(scope, security) == connector:
            return
        self._authorize(security, scope, Action.MANAGE_SPACE, "continuity_relation")

    def _connector_for_actor(self, scope: Scope, security) -> str:
        """The connector that submitted this request ("" when the caller is not one).

        Only enabled connectors that cover the current project count; if the same actor is
        behind several of them the attribution is ambiguous and we refuse to guess.
        """
        actor = asdict(security.auth.actor)
        matches = []
        for name, configured in (self._continuity_connectors or {}).items():
            if configured.get("actor") != actor:
                continue
            if not configured.get("enabled", True):
                continue
            if scope.space not in configured.get("projects", []):
                continue
            matches.append(name)
        if len(matches) > 1:
            raise ValidationError(
                "multiple enabled connectors match this principal: " + ", ".join(sorted(matches))
            )
        return matches[0] if matches else ""

    def _continuity_batch(
        self,
        entries: list[tuple],
        *,
        security: RequestSecurityContext,
        stop_on_error: bool = False,
    ) -> list[tuple[int, list[MemoryUnit] | Exception]]:
        """One five-phase reliable write for every continuity item in a batch.

        ``entries`` are ``(index, BatchWriteItem)``. Every item is authorized and counted
        before the batch starts, so a rejected item never half-writes the batch.
        """
        prepared = []
        rejected: list[tuple[int, Exception]] = []
        position = 0
        for position, (index, item) in enumerate(entries):
            continuity = item.continuity
            try:
                # 逐项校验：任何一项不合格只拒绝它自己，不影响同批其他项。
                self._authorize(security, item.scope, Action.WRITE, "add")
                service = self._role_service(item.scope)
                connector = service.binding_connector(
                    item.scope, continuity.task_id, continuity.binding_id
                )
                configured = self._continuity_connectors.get(connector, {})
                actor = asdict(security.auth.actor)
                if (
                    configured.get("actor") != actor
                    or not configured.get("enabled", connector in self._continuity_connectors)
                    or item.scope.space not in configured.get("projects", [])
                ):
                    raise PermissionDeniedError("add")
                conflicting = {"infer", "procedural", "middle"} & set(
                    item.system_metadata or {}
                )
                if conflicting:
                    raise ValidationError(
                        "continuity write does not accept system_metadata keys: "
                        + ", ".join(sorted(conflicting))
                    )
                expected_key = self._continuity_operation_id(item.scope, continuity)
                if item.idempotency_key and item.idempotency_key != expected_key:
                    # 幂等键不一致只是这一项的问题，整批继续。
                    raise ValidationError(
                        "idempotency_key must equal the continuity event key"
                    )
            except AgentMemoryError as exc:
                rejected.append((index, exc))
                if stop_on_error:
                    # 只跳过失败项**之后**的项：失败项之前的合法项已经通过校验，必须
                    # 继续写入，否则它们的下标拿不到结果（collect_batch_result 会 KeyError）。
                    rejected.extend(
                        (later, _Skipped()) for later, _ in entries[position + 1:]
                    )
                    break
                continue
            duty_ids = [
                str(tag).removeprefix("duty:")
                for tag in (item.tags or [])
                if isinstance(tag, str) and tag.startswith("duty:")
            ]
            prepared.append(
                {
                    "index": index,
                    "item": item,
                    "connector": connector,
                    # 事件身份：write_batch 只收到待写事件，必须靠它找回同一条目。
                    "operation_id": expected_key,
                    "event": ContinuityEvent(
                        task_id=continuity.task_id,
                        binding_id=continuity.binding_id,
                        event_id=continuity.event_id,
                        content=item.content,
                        kind=continuity.kind,
                        classification_confidence=continuity.classification_confidence,
                        evidence_refs=list(continuity.evidence_refs),
                        source_locator=continuity.source_locator,
                        claimed_author=continuity.claimed_author,
                        duty_ids=duty_ids,
                    ),
                    "request_fingerprint": fingerprint(
                        {
                            "content": item.content,
                            "source": None if item.source is None else item.source.value,
                            "assets": list(item.assets or []),
                            "tags": list(item.tags or []),
                            "system_metadata": dict(item.system_metadata or {}),
                            "user_metadata": dict(item.user_metadata or {}),
                            "occurred_at": (
                                None if item.occurred_at is None else item.occurred_at.isoformat()
                            ),
                            "event_time": (
                                None if item.event_time is None else item.event_time.isoformat()
                            ),
                        }
                    ),
                }
            )
        results: list[tuple[int, list[MemoryUnit] | Exception]] = list(rejected)
        handled = {index for index, _result in results}

        def skip_unhandled() -> None:
            """为尚未有结果的项补 Skipped：绝不能留空洞，否则上层按 range 取会 KeyError。"""
            for index, _item in entries:
                if index in handled:
                    continue
                results.append((index, _Skipped()))
                handled.add(index)

        if not prepared:
            skip_unhandled()
            return results
        # 同一 record_batch 只能服务一个项目空间；按**输入顺序**切成连续段，逐段写入。
        # 不能按全局分组：交错的 scope 会打乱副作用顺序，也可能在停批时留下空洞。
        runs: list[list[dict]] = []
        for entry in prepared:
            if runs and _same_scope(runs[-1][0]["item"].scope, entry["item"].scope):
                runs[-1].append(entry)
            else:
                runs.append([entry])

        for run in runs:
            scope = run[0]["item"].scope
            service = self._role_service(scope)
            failed = False

            def write_batch(writes, _run=run, _scope=scope):
                return asyncio.run(
                    self._add_many_impl(writes, _scope, _run, security=security)
                )

            try:
                receipts = service.record_batch(
                    scope,
                    [entry["event"] for entry in run],
                    [entry["connector"] for entry in run],
                    self._role_reader(security, scope),
                    write_batch,
                    request_fingerprints=[entry["request_fingerprint"] for entry in run],
                    # 派生构建需要「写后可读的精确元数据查询」，与单条路径同一口径。
                    query_output=(
                        lambda output_key: (
                            self.list(
                                scope,
                                security=security,
                                limit=2,
                                filters={
                                    "system_metadata.continuity_output": {"eq": output_key}
                                },
                            ).items
                        )
                    )
                    if self._queries.supports_exact_metadata_lookup
                    else None,
                )
            except AgentMemoryError as exc:
                # 批次级失败（例如任务在预检后被关闭）无法按条归属，统一记为该项错误。
                for entry in run:
                    results.append((entry["index"], exc))
                    handled.add(entry["index"])
                failed = True
            else:
                for entry, receipt in zip(run, receipts):
                    status = receipt.get("status")
                    if status == "committed":
                        units = []
                        for ref in receipt.get("refs") or []:
                            link = MemoryRef.parse(ref)
                            units.append(self.get(link.unit_id, link.scope, security=security))
                        results.append((entry["index"], units))
                    else:
                        failed = True
                        results.append(
                            (
                                entry["index"],
                                WriteIncompleteError(
                                    operation_id=receipt.get("operation_id", ""),
                                    status=str(status),
                                    refs=tuple(
                                        MemoryRef.parse(json.dumps(ref))
                                        for ref in receipt.get("refs") or []
                                    ),
                                    retry_action=(
                                        "retry_original_write"
                                        if status == "partial"
                                        else "recover_then_retry"
                                    ),
                                ),
                            )
                        )
                    handled.add(entry["index"])
            if failed and stop_on_error:
                skip_unhandled()
                return results
        return results

    async def _add_many_impl(self, writes, scope, prepared, *, security):
        """Write several continuity bodies through one engine batch call.

        ``writes`` are :class:`ContinuityBatchWrite` items carrying their own event
        identity, so pairing never depends on position: a retry only sends the events
        without a receipt, while ``prepared`` holds the whole run.

        Derived outputs are flagged ``derived``: like the single-item ``_role_writer``
        they keep the source location but must not inherit the source event's occurrence
        time, and each one is audited as its own write.
        """
        by_operation: dict[str, list[dict]] = {}
        for entry in prepared:
            by_operation.setdefault(entry["operation_id"], []).append(entry)

        resolved: list[tuple[object, dict]] = []
        for write in writes:
            candidates = by_operation.get(write.operation_id) or []
            if not candidates:
                raise ValidationError(
                    "batch write item does not match any event in this run"
                )
            resolved.append((write, candidates.pop(0)))

        items = []
        for write, entry in resolved:
            item = entry["item"]
            target = item.scope
            # 与单条 _role_writer 一致：接续写入固定 infer/middle=false。
            merged = {**(write.metadata or {}), **(item.system_metadata or {}),
                      "infer": "false", "middle": "false"}
            items.append(
                BatchWriteItem(
                    content=write.content,
                    scope=target,
                    source=item.source or Modality.TEXT,
                    assets=list(item.assets or []),
                    tags=item.tags,
                    system_metadata=_with_author_marks(
                        merged,
                        self._resource_principal(security, Action.WRITE, target),
                    ),
                    user_metadata=item.user_metadata,
                    occurred_at=item.occurred_at,
                    # 派生输出是按构建时刻生成的新记忆，不继承源事件的发生时间。
                    event_time=None if write.derived else item.event_time,
                    source_ref=item.continuity.source_locator or "",
                )
            )
        result = await self._commands.batch_write(items, continue_on_error=False)
        produced = []
        for outcome, (write, entry) in zip(result.outcomes, resolved):
            if outcome.error:
                raise WriteIncompleteError(
                    operation_id="",
                    status="unknown",
                    refs=(),
                    retry_action="recover_then_retry",
                )
            # 派生输出是独立写入的新记忆，和单条路径一样有自己的 add 审计。
            self._log(
                security.auth.actor,
                "add",
                str(write.metadata.get("continuity_output", ""))
                if write.derived
                else entry["event"].event_id,
                target_scope=entry["item"].scope,
            )
            produced.append([_strip_transient_metadata(unit) for unit in outcome.units])
        return produced

    def _continuity_operation_id(self, scope: Scope, continuity: ContinuityWrite) -> str:
        """The event idempotency key shared with ``ContinuityService.record``."""
        service = self._role_service(scope)
        connector = service.binding_connector(scope, continuity.task_id, continuity.binding_id)
        return service.event_key(connector, continuity.binding_id, continuity.event_id)

    def _continuity_add(
        self,
        content: str,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        continuity: ContinuityWrite,
        tags: list[str] | None,
        event_time,
        idempotency_key: str = "",
        source=None,
        assets: list[str] | None = None,
        system_metadata: dict | None = None,
        user_metadata: dict | None = None,
        occurred_at=None,
    ) -> list[MemoryUnit]:
        """Reliable continuity write through ``add``: idempotent event + task background.

        Returns the saved memory units on a committed event receipt; raises
        ``WriteIncompleteError`` while the receipt is unknown or partial so a caller can
        never mistake an incomplete write for success.
        """
        # 合并入口必须透传普通 add 的参数；不支持的组合显式拒绝，不静默丢弃
        # （静默丢弃的失效方向是「调用方以为元数据生效了」）。
        conflicting = {"infer", "procedural", "middle"} & set(system_metadata or {})
        if conflicting:
            raise ValidationError(
                "continuity write does not accept system_metadata keys: "
                + ", ".join(sorted(conflicting))
            )
        service = self._role_service(scope)
        self._authorize(security, scope, Action.WRITE, "add")
        connector = service.binding_connector(scope, continuity.task_id, continuity.binding_id)
        if idempotency_key:
            expected = service.event_key(
                connector, continuity.binding_id, continuity.event_id
            )
            if idempotency_key != expected:
                raise ValidationError("idempotency_key must equal the continuity event key")
        configured = self._continuity_connectors.get(connector, {})
        actor = asdict(security.auth.actor)
        if (
            configured.get("actor") != actor
            or not configured.get("enabled", connector in self._continuity_connectors)
            or scope.space not in configured.get("projects", [])
        ):
            raise PermissionDeniedError("add")
        read = self._role_reader(security, scope)
        # 经验条目的适用职责只以 `duty:<id>` 标签承载（ContinuityWrite 无独立字段），
        # 这里必须还原回事件，否则视图里经验条目的 duties 永远为空。
        duty_ids = [
            str(tag).removeprefix("duty:")
            for tag in (tags or [])
            if isinstance(tag, str) and tag.startswith("duty:")
        ]
        event = ContinuityEvent(
            task_id=continuity.task_id,
            binding_id=continuity.binding_id,
            event_id=continuity.event_id,
            content=content,
            kind=continuity.kind,
            classification_confidence=continuity.classification_confidence,
            evidence_refs=list(continuity.evidence_refs),
            source_locator=continuity.source_locator,
            claimed_author=continuity.claimed_author,
            duty_ids=duty_ids,
        )
        # 幂等摘要必须覆盖所有会改变持久化内容的参数：同一 (task, binding, event) 改
        # event_time/metadata/时间后重试是**不同请求**，应冲突而不是复用旧回执。
        request_fingerprint = fingerprint(
            {
                "content": content,
                "source": None if source is None else source.value,
                "assets": list(assets or []),
                "tags": list(tags or []),
                "system_metadata": dict(system_metadata or {}),
                "user_metadata": dict(user_metadata or {}),
                "occurred_at": None if occurred_at is None else occurred_at.isoformat(),
                "event_time": None if event_time is None else event_time.isoformat(),
            }
        )
        receipt = service.record(
            scope,
            event,
            connector,
            read,
            self._role_writer(
                scope, security, event_time=event_time, tags=tags,
                source_ref=continuity.source_locator or "",
                source=source, assets=assets, system_metadata=system_metadata,
                user_metadata=user_metadata, occurred_at=occurred_at,
            ),
            query_output=(
                lambda output_key: (
                    self.list(
                        scope,
                        security=security,
                        limit=2,
                        filters={"system_metadata.continuity_output": {"eq": output_key}},
                    ).items
                )
            )
            if self._queries.supports_exact_metadata_lookup
            else None,
            request_fingerprint=request_fingerprint,
        )
        self._log(security.auth.actor, "add", continuity.event_id, target_scope=scope)
        if receipt["status"] != "committed":
            raise WriteIncompleteError(
                operation_id=receipt["operation_id"],
                status=receipt["status"],
                refs=tuple(MemoryRef.parse(item) for item in receipt.get("refs", [])),
                retry_action=(
                    "retry_original_write"
                    if receipt["status"] == "partial"
                    else "recover_then_retry"
                ),
            )
        return [read(MemoryRef.parse(item)) for item in receipt.get("refs", [])]

    def continuity_relation(
        self,
        scope: Scope,
        relation: RelationRecord,
        expected_revision: int,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        self._authorize(security, scope, Action.WRITE, "continuity_relation")
        service = self._role_service(scope)
        read = (
            self._role_management_reader(security, scope)
            if relation.status == RelationStatus.REVOKED
            else self._role_reader(security, scope)
        )
        if expected_revision:
            head = service.relation_head(scope, relation.relation_id)
            current = head.get("current") if head else None
            if current:
                existing = RelationRecord.parse(current)
                # 登记侧：只有提交连接本身或项目治理角色能改这条关系（撤回同样适用）。
                # 旧关系头没有 connector，就只剩治理角色能改——否则它谁都能改。
                self._require_relation_submitter(scope, security, head.get("connector", ""))
                # 内容侧：两端记忆的 UPDATE 权保持现状。
                for ref in (existing.source, existing.target):
                    unit = self._role_management_reader(security, scope)(ref)
                    self._authorize(
                        security,
                        unit.scope,
                        Action.UPDATE,
                        "update",
                        unit.id,
                        context=_unit_snapshot_permission_context(unit),
                    )
        result = service.relation(
            scope,
            relation,
            expected_revision,
            read,
            read_previous=self._role_management_reader(security, scope),
            # D3：关系头必须记住提交连接，隔离/停用该连接后关系才不可用。
            connector=self._connector_for_actor(scope, security),
        )
        self._log(
            security.auth.actor, "continuity_relation", relation.relation_id, target_scope=scope
        )
        return result

    def continuity_recover(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        recovery: ContinuityRecovery | None = None,
    ) -> dict:
        self._authorize(security, scope, Action.WRITE, "continuity_recover")
        service = self._role_service(scope)
        read = self._role_management_reader(security, scope)
        if recovery is not None:
            connector = service.recovery_owner(scope, recovery)
            if recovery.kind in {"event", "derivation"} and (
                self._continuity_connectors.get(connector, {}).get("actor")
                != asdict(security.auth.actor)
            ):
                raise PermissionDeniedError("continuity_recover")
            for ref in recovery.refs:
                unit = read(ref)
                self._authorize(
                    security,
                    unit.scope,
                    Action.UPDATE,
                    "update",
                    unit.id,
                    context=_unit_snapshot_permission_context(unit),
                )
            result = service.recover(scope, recovery, read)
        else:
            result = service.repair(scope, read)
        self._log(security.auth.actor, "continuity_recover", "", target_scope=scope)
        return result

    def quarantine_source(
        self,
        scope: Scope,
        source_key: str,
        reason: str,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        self._authorize(security, scope, Action.MANAGE_SPACE, "quarantine_source")
        service = self._role_service(scope)
        record = service.quarantine_source(
            scope, source_key, reason, operator=fingerprint(asdict(security.auth.actor))
        )
        self._log(
            security.auth.actor,
            "quarantine_source",
            source_key,
            target_scope=scope,
            detail={"reason": reason},
        )
        return record

    def release_source(
        self,
        scope: Scope,
        source_key: str,
        *,
        security: RequestSecurityContext,
    ) -> bool:
        self._authorize(security, scope, Action.MANAGE_SPACE, "release_source")
        service = self._role_service(scope)
        released = service.release_source(scope, source_key)
        self._log(security.auth.actor, "release_source", source_key, target_scope=scope)
        return released
