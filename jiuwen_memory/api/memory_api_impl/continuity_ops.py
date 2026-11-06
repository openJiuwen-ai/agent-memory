"""PEP and thin DTO adapters for the optional S10 application service."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import UTC, datetime

from jiuwen_memory.common.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import ContentLayers, Context, Modality, Scope
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    RoleViewQuery,
    TaskRequest,
    canonical,
    fingerprint,
)
from jiuwen_memory.retrieval.types import DisclosureLevel

from .local_support import (
    _space_permission_context,
    _space_scope,
    _space_target_id,
    _unit_snapshot_permission_context,
)

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
        if self._continuity is not None and self._continuity.source_quarantine(
            scope, connector
        ) is not None:
            return False
        return True

    def _guard_continuity_snapshot(self, unit, security):
        """Recheck explicit dependencies on every public snapshot read, even if disabled."""
        metadata = unit.system_metadata
        connector = metadata.get("continuity_connector")
        if connector and not self._source_available(unit.scope, connector):
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
            elif metadata.get("continuity_source_refs"):
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
            if metadata.get("continuity_kind") == "relation" and relation.resolution_refs:
                if self._continuity is None:
                    raise PermissionDeniedError("resolution verification unavailable")
                self._continuity.validate_resolution(
                    unit.scope, relation, self._role_reader(security, unit.scope)
                )
        finally:
            _READ_DEPTH.reset(token)

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
            if (metadata.get("continuity_kind") == "relation"
                    or metadata.get("continuity_source_refs")):
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

    def _role_writer(self, scope, security, *, event_time=None, verbatim_time=None):
        def write(content, metadata):
            return asyncio.run(self._add_impl(
                content,
                scope,
                security=security,
                system_metadata={"infer": "false", "middle": "false"},
                continuity_metadata=metadata,
                # Source events carry their own time; a published copy is the same
                # record. Derived outputs do not inherit it (F10 4.2).
                event_time=(event_time if metadata.get("continuity_event") else verbatim_time),
            ))

        return write

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

        def conditional_update(ref, expected_digest, content, metadata):
            # The same PEP as an ordinary update; WRITE on the project does not grant
            # permission to overwrite an existing memory owned by another actor.
            expected = replace(ref, content_digest=expected_digest)
            unit = self._role_reader(security, scope)(expected)
            self._authorize(
                security, unit.scope, Action.UPDATE, "update", unit.id,
                context=_unit_snapshot_permission_context(unit),
            )
            # Frozen plans carry one text body, not segment addresses. Never guess
            # which segment or multimodal projection to overwrite (including old plans).
            if (len(unit.segments) != 1 or unit.segments[0].assets
                    or unit.segments[0].source != Modality.TEXT):
                raise ValidationError("derivation update requires a single plain-text segment")
            changed = replace(
                unit,
                segments=[replace(unit.segments[0], content=content)],
                layers=ContentLayers(),
                system_metadata={**unit.system_metadata, **metadata},
            )
            applied = self._commands.conditional_update(changed, expected_digest)
            if applied:
                self._log(security.auth.actor, "update", unit.id, target_scope=unit.scope)
            return applied

        result = service.record(
            scope,
            event,
            connector,
            self._role_reader(security, scope),
            self._role_writer(
                scope, security,
                event_time=(datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
                            if event.occurred_at else None),
            ),
            query_output=(lambda output_key: self.list(
                scope, security=security, limit=2,
                filters={"system_metadata.continuity_output": {"eq": output_key}},
            ).items) if self._queries.supports_exact_metadata_lookup else None,
            conditional_update=conditional_update,
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
            if recovery.kind in {"event", "derivation"} and (
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

    def role_quarantine_source(
        self,
        scope: Scope,
        source_key: str,
        reason: str,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        self._authorize(security, scope, Action.MANAGE_SPACE, "role_quarantine_source")
        service = self._role_service(scope)
        record = service.quarantine_source(
            scope, source_key, reason, operator=fingerprint(asdict(security.auth.actor))
        )
        self._log(
            security.auth.actor,
            "role_quarantine_source",
            source_key,
            target_scope=scope,
            detail={"reason": reason},
        )
        return record

    def role_release_source(
        self,
        scope: Scope,
        source_key: str,
        *,
        security: RequestSecurityContext,
    ) -> bool:
        self._authorize(security, scope, Action.MANAGE_SPACE, "role_release_source")
        service = self._role_service(scope)
        released = service.release_source(scope, source_key)
        self._log(security.auth.actor, "role_release_source", source_key, target_scope=scope)
        return released

    def role_mark_source_cutover(
        self,
        scope: Scope,
        connector: str,
        context_id: str,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        self._authorize(security, scope, Action.MANAGE_SPACE, "role_mark_source_cutover")
        service = self._role_service(scope)
        result = service.mark_source_cutover(scope, connector, context_id)
        self._log(
            security.auth.actor,
            "role_mark_source_cutover",
            connector,
            target_scope=scope,
            detail={"context_id": context_id},
        )
        return result

    def role_publish(
        self,
        scope: Scope,
        source: MemoryRef,
        reason: str,
        *,
        security: RequestSecurityContext,
    ) -> dict:
        """Explicitly publish a private record into the shared project space.

        The publisher needs WRITE on the project, READ **and SHARE** on the source's own
        scope (a reader who cannot re-share must not broaden visibility), and the shared
        record keeps the verbatim content plus origin/provenance and justification —
        publishing never auto-summarizes private text into public facts.
        """
        if not reason.strip():
            raise ValidationError("publish requires a non-empty justification")
        self._authorize(security, scope, Action.WRITE, "role_publish")
        unit = self._role_reader(security, scope)(source)
        if unit.scope.org != scope.org:
            raise ValidationError("publish requires the same organization")
        # Re-sharing private content into the shared space requires SHARE on the source
        # scope, not merely READ; otherwise a reader broadens visibility without grant.
        source_scope = _space_scope(unit.scope.org, unit.scope.space)
        self._authorize(
            security,
            source_scope,
            Action.SHARE,
            "role_publish_share",
            _space_target_id(source_scope.org, source_scope.space),
            context=_space_permission_context("space_member", source_scope),
        )
        source_time = unit.temporal.t_event
        published = self._role_writer(
            scope, security,
            verbatim_time=(source_time if source_time is not None
                           and source_time.utcoffset() is not None else None),
        )(
            unit.content,
            {
                "continuity_kind": "statement",
                "continuity_published_source": canonical(asdict(source)),
                "continuity_publish_reason": reason,
            },
        )
        if len(published) != 1:
            raise ValidationError("publish requires one shared record")
        ref = MemoryRef.of(published[0])
        self._log(
            security.auth.actor,
            "role_publish",
            ref.unit_id,
            target_scope=scope,
            detail={"source": canonical(asdict(source)), "reason": reason},
        )
        return {"ref": asdict(ref), "source": asdict(source)}
