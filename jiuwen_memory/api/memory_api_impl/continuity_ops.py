"""PEP and thin DTO adapters for the optional S10 application service."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime

from jiuwen_memory.common.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    RoleViewQuery,
    TaskRequest,
    fingerprint,
)
from jiuwen_memory.retrieval.types import DisclosureLevel

from .local_support import _unit_snapshot_permission_context

_MANAGE_RELATION = ContextVar("continuity_manage_relation", default=False)
_READ_DEPTH = ContextVar("continuity_read_depth", default=0)


class ContinuityOpsMixin:
    def _guard_continuity_snapshot(self, unit, security):
        """Recheck explicit dependencies on every public snapshot read, even if disabled."""
        metadata = unit.system_metadata
        connector = metadata.get("continuity_connector")
        if connector and not self._continuity_connectors.get(connector, {}).get(
            "enabled", connector in self._continuity_connectors
        ):
            raise PermissionDeniedError("source unavailable")
        if _MANAGE_RELATION.get() and metadata.get("continuity_kind") == "relation":
            return
        refs = []
        try:
            if metadata.get("continuity_kind") == "relation":
                relation = RelationRecord.parse(json.loads(unit.content))
                refs = [
                    relation.source,
                    relation.target,
                    *relation.evidence_refs,
                    *relation.resolution_refs,
                ]
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
            if metadata.get("continuity_kind") == "relation" and relation.resolution_refs:
                if self._continuity is None:
                    raise PermissionDeniedError("resolution verification unavailable")
                self._continuity.validate_resolution(
                    unit.scope, relation, self._role_reader(security, unit.scope)
                )
        finally:
            _READ_DEPTH.reset(token)

    def _guard_continuity_search(self, result):
        # Ordinary retrieval has no per-item real Scope/path contract. Structured records
        # with dependency-bearing payloads are disclosed only through guarded point reads.
        import json

        from jiuwen_memory.common.type_def import ChannelError, RecallChannel

        visible = []
        for item in result.items:
            metadata = item.system_metadata
            connector = metadata.get("continuity_connector")
            if connector and not self._continuity_connectors.get(connector, {}).get(
                "enabled", connector in self._continuity_connectors
            ):
                continue
            if metadata.get("continuity_kind") == "relation":
                continue
            if metadata.get("continuity_envelope"):
                try:
                    if json.loads(str(metadata["continuity_envelope"])).get("evidence_refs"):
                        continue
                except (ValueError, TypeError):
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
        if not scope.org or not scope.space or scope.user or scope.agent or scope.session:
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
            if connector and not self._continuity_connectors.get(connector, {}).get(
                "enabled", connector in self._continuity_connectors
            ):
                raise PermissionDeniedError("source unavailable")
            if MemoryRef.of(unit) != ref:
                raise ConflictError("reference", message="reference content changed")
            now = datetime.now(UTC)
            if unit.lifecycle.value != "active":
                raise ConflictError("reference", message="reference is not active")
            if (unit.temporal.t_valid and unit.temporal.t_valid > now) or (
                unit.temporal.t_invalid and unit.temporal.t_invalid <= now
            ):
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

    def _role_writer(self, scope, security):
        return lambda content, metadata: self.add(
            content,
            scope,
            security=security,
            system_metadata={**metadata, "infer": "false", "middle": "false"},
        )

    def role_task(
        self,
        scope: Scope,
        request: TaskRequest,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        reading = request.operation in {"get", "find"}
        if reading:
            self._authorize(security, scope, Action.READ, "role_task_read")
        else:
            self._authorize(security, scope, Action.WRITE, "role_task_write")
        service = self._role_service(scope)
        owner = fingerprint(asdict(security.auth.actor))
        if request.operation not in {"create", "get", "find"}:
            if service.task_owner(scope, request.task_id) != owner:
                raise PermissionDeniedError("role_task_write")
        if request.operation == "bind":
            connector = self._continuity_connectors.get(request.connector_id)
            if not connector or connector.get("actor", {}).get("org") != scope.org:
                raise ValidationError("connector is not configured for this organization")
            if scope.space not in connector.get("projects", []):
                raise ValidationError("connector is not configured for this project")
        result = service.task(scope, request, owner, self._role_reader(security, scope))
        self._log(security.auth.actor, "role_task", request.task_id, target_scope=scope)
        return result

    def role_record(
        self,
        scope: Scope,
        event: ContinuityEvent,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        self._authorize(security, scope, Action.WRITE, "role_record")
        service = self._role_service(scope)
        actor = asdict(security.auth.actor)
        connector = service.binding_connector(scope, event.task_id, event.binding_id)
        # Connection attribution is from trusted configuration, never an event field.
        configured = self._continuity_connectors.get(connector, {})
        if (
            configured.get("actor") != actor
            or not configured.get("enabled", connector in self._continuity_connectors)
            or scope.space not in configured.get("projects", [])
        ):
            raise PermissionDeniedError("role_record")
        result = service.record(
            scope,
            event,
            connector,
            self._role_reader(security, scope),
            self._role_writer(scope, security),
        )
        self._log(security.auth.actor, "role_record", event.event_id, target_scope=scope)
        return result

    def role_relation(
        self,
        scope: Scope,
        relation: RelationRecord,
        expected_revision: int,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        self._authorize(security, scope, Action.WRITE, "role_relation")
        service = self._role_service(scope)
        read = (
            self._role_management_reader(security, scope)
            if relation.status == RelationStatus.REVOKED
            else self._role_reader(security, scope)
        )
        if expected_revision:
            head_ref = service.relation_head_ref(scope, relation.relation_id)
            if head_ref:
                ref = MemoryRef.parse(head_ref)
                # Revisions of other authors' relations require UPDATE, not just WRITE.
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
            self._role_writer(scope, security),
            read_previous=self._role_management_reader(security, scope),
        )
        self._log(security.auth.actor, "role_relation", relation.relation_id, target_scope=scope)
        return result

    def role_view(
        self,
        scope: Scope,
        request: RoleViewQuery,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        self._authorize(security, scope, Action.READ, "role_view")
        service = self._role_service(scope)
        read = self._role_reader(security, scope)

        def discover(query, limit):
            results = self.search(
                query,
                Context(scope),
                security=security,
                top_k=limit,
                disclosure=DisclosureLevel.L2,
            )
            units = []
            for item in results.items:
                # Schema entity views are not memory IDs; unresolved candidates are omitted.
                try:
                    unit = self.get(item.unit_id, scope, security=security)
                    read(MemoryRef.of(unit))
                    units.append(unit)
                except (PermissionDeniedError, ConflictError, NotFoundError):
                    continue
            return units

        result = service.view(scope, request, read, discover)
        self._log(security.auth.actor, "role_view", request.task_id, target_scope=scope)
        return result

    def role_rebuild(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        recovery: ContinuityRecovery | None = None,
    ) -> dict:
        self._authorize(security, scope, Action.WRITE, "role_rebuild")
        service = self._role_service(scope)
        read = self._role_management_reader(security, scope)
        if recovery is not None:
            connector = service.recovery_owner(scope, recovery)
            if recovery.kind == "event" and (
                self._continuity_connectors.get(connector, {}).get("actor")
                != asdict(security.auth.actor)
            ):
                raise PermissionDeniedError("role_rebuild")
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
        self._log(security.auth.actor, "role_rebuild", "", target_scope=scope)
        return result
