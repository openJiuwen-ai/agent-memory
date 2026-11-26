# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authorization boundary for document-backed collective memory requests."""
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from jiuwen_memory.api.collective import (
    COLLECTIVE_MANAGER_ID,
    collective_coordinates,
    dimensions_of,
    select_collective_coordinates,
)
from jiuwen_memory.common.errors import (
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.authorization.collective_read import COLLECTIVE_MEMBER_LIMIT
from jiuwen_memory.common.security.authorization.scope_rules import PrincipalPath, scope_covers
from jiuwen_memory.common.security.space_decision import ATTR_PRINCIPAL_PATH
from jiuwen_memory.common.security.types import (
    Action,
    AuthorizationEnvironment,
    RequestSecurityContext,
    ResourceDescriptor,
    Role,
)
from jiuwen_memory.common.type_def import MD_TITLE_KEY, Context, MemoryUnit, Modality, Scope, Segment
from jiuwen_memory.common.type_def.filter import and_merge
from jiuwen_memory.common.type_def.memory_filter import is_collective_metadata
from jiuwen_memory.control.collective.cross_space_recall import SpaceRecallTarget
from jiuwen_memory.control.executor import ExecutorLane
from jiuwen_memory.control.pipeline import route_value_of
from jiuwen_memory.retrieval import RetrievalQuery, RetrievalResult
from jiuwen_memory.retrieval.cross_space import space_error
from jiuwen_memory.storage.collective_members import Dimension, Member, MemberStatus, MemberStore
from jiuwen_memory.storage.markdown_impl._md_path import collective_landing_metadata

from .local_support import (
    _recall_permission_context,
    _routing_clauses_of,
    _TrustedRequestTarget,
    _type_isolation_clause,
    _with_author_marks,
)

_logger = get_logger(__name__)


class CollectiveOpsMixin:
    def _member_store(self) -> MemberStore:
        if self._collective_members is None:
            raise UnsupportedCapabilityError("collective_memory", "document", "MemoryAPI")
        return self._collective_members

    def _guard_collective_unit(self, unit: MemoryUnit, security: RequestSecurityContext) -> None:
        if not is_collective_metadata(unit.system_metadata):
            return
        if unit.system_metadata.get("shareable") is True:
            return
        coordinates = self._require_collective_subject(
            unit.scope, security, Action.READ, "read unit",
        )
        current = set(dimensions_of(unit.scope, coordinates))
        active = set(self._member_store().active_dimensions(unit.scope))
        if not any(unit.system_metadata.get(d.kind) == d.id for d in current & active):
            raise PermissionDeniedError("memory is limited to its current collective context")

    async def _add_collective(
        self, sources, scope, *, security, system_metadata, user_metadata, tags,
        occurred_at, event_time,
    ) -> list[MemoryUnit]:
        if self._privacy is not None and self._document_mode:
            raise ValidationError("Collective writes are unavailable for protected documents")
        coordinates = self._require_collective_subject(scope, security, Action.WRITE, "add")
        options = dict(system_metadata or {})
        coordinates = select_collective_coordinates(coordinates, options.pop("coords", None))
        if options.keys() - {"infer", "shareable", "pipeline"}:
            raise ValidationError("collective add accepts only infer and shareable system metadata")
        if options.get("pipeline", "collective_turn") != "collective_turn":
            raise ValidationError("collective add requires the collective_turn pipeline")
        if str(options.get("infer", True)).lower() != "true":
            raise ValidationError("collective add requires infer=True")
        shareable = options.get("shareable")
        if "shareable" in options and not isinstance(shareable, bool):
            raise ValidationError("shareable must be a boolean")
        if not isinstance(sources, (list, tuple)) or not 0 < len(sources) <= 64:
            raise ValidationError("collective sources must contain between 1 and 64 segments")
        segments = []
        seen_ids = set()
        total_length = 0
        for value in sources:
            if not isinstance(value, Mapping) or set(value) not in (
                {"id", "role", "text"}, {"id", "role", "text", "selection"},
            ):
                raise ValidationError("invalid collective extraction source")
            source_id, role, text = value["id"], value["role"], value["text"]
            if role not in ("user", "agent", "assistant") or not all(
                isinstance(part, str) and part.strip() for part in (source_id, text)
            ):
                raise ValidationError("invalid collective extraction source")
            metadata = {"source_role": role}
            if "selection" in value:
                selection = value["selection"]
                if role != "user" or not isinstance(selection, Mapping):
                    raise ValidationError("invalid collective user selection")
                valid_text = all(
                    isinstance(part, str) and len(part) <= 2000 for part in selection.values()
                )
                expected_keys = {"question", "description"}
                if set(selection) != expected_keys or not valid_text or not selection["question"].strip():
                    raise ValidationError("invalid collective user selection")
                total_length += sum(map(len, selection.values()))
                metadata["source_selection"] = {
                    key: self._privacy.preview(part) if self._privacy is not None and part else part
                    for key, part in selection.items()
                }
            total_length += len(text)
            if source_id in seen_ids or total_length > 96000:
                raise ValidationError("collective sources exceed extraction limits or repeat an id")
            seen_ids.add(source_id)
            text = self._privacy.preview(text) if self._privacy is not None else text
            if text.strip():
                segments.append(MemoryUnit(
                    id=source_id, scope=scope, segments=[Segment(content=text)],
                    system_metadata=metadata,
                ))

        await self._run_blocking(ExecutorLane.IO, self._ensure_audit_integrity_healthy)
        auth = await self._run_blocking(
            ExecutorLane.IO, self._authorize_and_check, security, scope, None,
            coords=None, metadata=options,
        )
        members = self._member_store()
        current = set(dimensions_of(scope, coordinates))
        active = current.intersection(members.active_dimensions(scope))
        destinations = {dimension.kind: dimension.id for dimension in active}
        if shareable is not False:
            if scope.agent and scope.agent != "main_agent":
                destinations["personal"] = scope.agent
            if scope.user and any(s.system_metadata["source_role"] == "user" for s in segments):
                destinations["user_profile"] = scope.user
        if not destinations or not segments:
            return []
        candidates = await self._run_blocking(
            ExecutorLane.LLM, self._collective_extractor, segments, destinations,
        )
        author = (
            self._resource_principal(security, Action.WRITE, scope)
            if security.auth.delegation_id else scope
        )
        written = []
        for candidate in candidates:
            destination = candidate.system_metadata.get("collective_destination")
            if destination not in destinations:
                continue
            key = destinations[destination]
            target = scope
            metadata = collective_landing_metadata(destination, key, shareable=shareable)
            if destination == "user_profile":
                target = Scope(org=scope.org, space=scope.space, user=scope.user)
            elif destination in coordinates:
                dimension = Dimension(scope.org, scope.space, destination, key)
                if dimension not in members.active_dimensions(scope):
                    continue
            if destination == "project" and candidate.system_metadata.get("memory_class") == "daily_memory":
                metadata["memory_class"] = "daily_memory"
            for field in ("team_label", MD_TITLE_KEY):
                if candidate.system_metadata.get(field):
                    metadata[field] = candidate.system_metadata[field]
            metadata = _with_author_marks(metadata, author)
            if self._collective_dedup is not None:
                unit = MemoryUnit(scope=target, segments=[Segment(content=candidate.content)],
                                  system_metadata=metadata)
                if await self._run_blocking(ExecutorLane.IO, self._collective_dedup, unit):
                    continue
            written.extend(await self._commands.write(
                candidate.content, target, Modality.TEXT, tags=tags,
                system_metadata=metadata, user_metadata=user_metadata,
                occurred_at=occurred_at, event_time=event_time,
            ))
        self._log(security.auth.actor, "add", target_scope=scope, detail=auth)
        return written

    def _require_collective_subject(
        self,
        scope: Scope,
        security: RequestSecurityContext,
        action: Action,
        operation: str,
    ) -> dict[str, str]:
        self._require_trusted_context(security, _TrustedRequestTarget(action, operation, scope))
        actor = security.auth.actor
        scope_error = "collective memory requires the authenticated subject's scope"
        if scope.org != actor.org or actor.space not in ("", scope.space) or scope.session:
            raise PermissionDeniedError(scope_error)
        identity_mismatch = (actor.user and scope.user != actor.user) or (
            actor.agent and scope.agent != actor.agent
        )
        if not security.auth.delegation_id and identity_mismatch:
            raise PermissionDeniedError(scope_error)
        coordinates = collective_coordinates(security)
        if coordinates is None:
            raise PermissionDeniedError("collective request has no trusted context")
        return coordinates

    def _search_collective(
        self,
        context: Context,
        query: RetrievalQuery,
        *,
        security: RequestSecurityContext,
    ) -> RetrievalResult:
        coordinates = self._require_collective_subject(
            context.scope, security, Action.READ, "search"
        )
        coordinates = select_collective_coordinates(coordinates, context.extensions.get("coords"))
        policy_context = _recall_permission_context(
            context, query.filters, self._authorizer.routing_fields(),
        )
        auth, _ = self._authorize_with_context(
            security, context.scope, Action.READ, "search",
            context=policy_context, require_space=bool(context.scope.space),
        )
        resource = self._resource_descriptor(context.scope, Action.READ, "", policy_context)
        plan = self._authorizer.authorize_collective_search(
            auth=security.auth,
            resource=ResourceDescriptor(
                Action.READ, "collective_search", context.scope, attributes=resource.attributes,
            ),
            environment=AuthorizationEnvironment.from_request(security, now=datetime.now(UTC)),
            memberships=self._member_store().contributors(context.scope),
            current=dimensions_of(context.scope, coordinates),
            limit=COLLECTIVE_MEMBER_LIMIT,
        )
        if plan is None:
            self._log(security.auth.actor, "search", target_scope=context.scope, decision="deny")
            raise PermissionDeniedError("authorization policy does not permit collective search")
        policy_clauses = _routing_clauses_of(policy_context, self._routing_fields_for("search"))
        if self._queries.retrieval_pipeline_filter(query.pipeline) is None:
            filters = and_merge(query.filters, policy_clauses)
            route = route_value_of(query.extensions, filters, self._route_key())
            policy_clauses.append(_type_isolation_clause(route))

        merged, failures = self._recall_authorized_targets(
            [
                SpaceRecallTarget(target.scope, (target.filters, *policy_clauses))
                for target in plan.targets
            ],
            query,
            bucket=lambda scope: json.dumps([scope.space, scope.user, scope.agent, scope.session]),
        )
        merged.errors.extend(failures)
        if plan.omitted:
            merged.errors.append(space_error(
                "collective", "FanoutLimit", f"{plan.omitted} contributor scopes were omitted",
            ))
            _logger.warning("collective recall omitted %d contributor scopes", plan.omitted)
        auth["collective_contributors"] = str(len(plan.targets) - 1)
        auth["collective_omitted"] = str(plan.omitted)
        auth["collective_targets"] = json.dumps([
            [target.scope.user, target.scope.agent, target.scope.session]
            for target in plan.targets if target.scope != context.scope
        ], separators=(",", ":"))
        self._log(security.auth.actor, "search", target_scope=context.scope, detail=auth)
        return merged

    def _collective_operator(
        self,
        dimension: Dimension,
        security: RequestSecurityContext,
        operation: str,
        operator_scope: Scope | None = None,
        *,
        initialize: bool = False,
        read_only: bool = False,
    ) -> Scope | None:
        target = Scope(org=dimension.org, space=dimension.space)
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.MANAGE_SPACE, operation, target)
        )
        actor = security.auth.actor
        auth = security.auth
        admin = not auth.delegation_id and (
            auth.role is Role.ROOT or (auth.role is Role.ADMIN and actor.org == dimension.org)
        )
        if admin:
            self._authorize(security, target, Action.MANAGE_SPACE, operation, require_space=False)
            return None
        if initialize or actor.org != dimension.org or actor.space not in ("", dimension.space):
            self._log(actor, operation, dimension.id, target_scope=target, decision="deny")
            raise PermissionDeniedError(operation)
        default = Scope(org=actor.org, space=dimension.space, user=actor.user, agent=actor.agent)
        operator = operator_scope if operator_scope is not None else default
        if (
            (operator.org, operator.space) != (dimension.org, dimension.space)
            or (auth.delegation_id and not read_only)
        ):
            self._log(actor, operation, dimension.id, target_scope=target, decision="deny")
            raise PermissionDeniedError(operation)
        _, effective = self._authorize_with_context(
            security,
            operator,
            Action.READ,
            operation,
            require_space=False,
        )
        if not auth.delegation_id:
            attributes = effective.metadata if effective is not None else {}
            path = PrincipalPath(
                attributes.get(ATTR_PRINCIPAL_PATH, PrincipalPath.USER_AGENT.value),
            )
            if not scope_covers(default, operator, principal_path=path):
                self._log(actor, operation, dimension.id, target_scope=target, decision="deny")
                raise PermissionDeniedError(operation)
        return operator

    def init_collective_dimension(
        self,
        dimension: Dimension,
        members: Sequence[Member],
        *,
        security: RequestSecurityContext,
        operator_scope: Scope | None = None,
    ) -> tuple[Member, ...]:
        operation = "init_collective_dimension"
        self._collective_operator(dimension, security, operation, operator_scope, initialize=True)
        self._ensure_audit_integrity_healthy()
        host_managed = security.auth.actor.user == COLLECTIVE_MANAGER_ID
        enrolled = self._member_store().initialize(dimension, members, host_managed=host_managed)
        self._log(security.auth.actor, operation, dimension.id)
        return enrolled

    def add_collective_members(
        self,
        dimension: Dimension,
        members: Sequence[Member],
        *,
        security: RequestSecurityContext,
        operator_scope: Scope | None = None,
    ) -> tuple[Member, ...]:
        operation = "add_collective_members"
        operator = self._collective_operator(dimension, security, operation, operator_scope)
        self._ensure_audit_integrity_healthy()
        enrolled = self._member_store().upsert(dimension, members, operator=operator)
        self._log(security.auth.actor, operation, dimension.id)
        return enrolled

    def remove_collective_members(
        self,
        dimension: Dimension | None,
        targets: Sequence[Scope],
        *,
        security: RequestSecurityContext,
        operator_scope: Scope | None = None,
    ) -> int:
        operation = "remove_collective_members"
        if dimension is None:
            return self._retire_collective_subjects(targets, security, operator_scope)
        operator = self._collective_operator(dimension, security, operation, operator_scope)
        self._ensure_audit_integrity_healthy()
        removed = self._member_store().depart(dimension, targets, operator=operator)
        self._log(security.auth.actor, operation, dimension.id)
        return removed

    def _retire_collective_subjects(self, targets, security, operator_scope) -> int:
        operation = "remove_collective_members"
        actor = security.auth.actor
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.MANAGE_SPACE, operation, actor),
        )
        if (
            operator_scope is not None or security.auth.delegation_id
            or security.auth.role not in (Role.ADMIN, Role.ROOT)
        ):
            raise PermissionDeniedError("retiring an expert requires an organization admin")
        if not targets or any(t.session or not t.agent or t.org != actor.org for t in targets):
            raise ValidationError("retirement requires stable experts in the current organization")
        homes = {(t.org, t.space) for t in targets}
        for org, space in homes:
            self._authorize(security, Scope(org=org, space=space), Action.MANAGE_SPACE,
                            operation, require_space=False)
        self._ensure_audit_integrity_healthy()
        members = self._member_store()
        removed = 0
        for subject in targets:
            for dimension in members.active_dimensions(subject):
                if dimension.kind in ("team", "group"):
                    removed += members.depart(dimension, [subject])
        self._log(actor, operation, detail={"departed_memberships": str(removed)})
        return removed

    def list_collective_members(
        self,
        dimension: Dimension,
        *,
        status: MemberStatus | None = None,
        security: RequestSecurityContext,
        operator_scope: Scope | None = None,
    ) -> tuple[Member, ...]:
        operation = "list_collective_members"
        operator = self._collective_operator(
            dimension, security, operation, operator_scope, read_only=True,
        )
        rows = self._member_store().members(dimension)
        if operator is not None and not any(
            row.scope == operator and row.status == "active" for row in rows
        ):
            self._log(security.auth.actor, operation, dimension.id, decision="deny")
            raise PermissionDeniedError(operation)
        self._log(security.auth.actor, operation, dimension.id)
        return tuple(row for row in rows if status is None or row.status == status)
