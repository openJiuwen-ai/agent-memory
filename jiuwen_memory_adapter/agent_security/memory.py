"""SDK 读取与 agent 文字投影之间的 BEFORE_MEMORY_EXPORT；不暴露原始 SDK 代理。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field, replace

from ._validation import has_exact_type
from .runtime import Actor, PayloadInspector, SecurityDenied, encode_payload
from .sources import SourceLedger, SourceReference, strongest


@dataclass(frozen=True)
class MemoryView:
    """只向 agent 释放此最小文本投影；来源和真实存储标识留在可信账本。"""

    reference: str
    content: str = field(repr=False)
    abstract: str = field(repr=False)
    overview: str = field(repr=False)


class SdkMemoryReader:
    """只支持本地公开 MemoryAPI 的 get/list/普通 search，拒绝未标记/聚合/附件视图。"""

    def __init__(
        self,
        api,
        sources: SourceLedger,
        inspector,
        *,
        provider: str,
        security_for,
        scope_for,
        authorize_use,
        protected_source_levels_for,
        detector_version_for=None,
        purpose="agent-memory-use",
        max_items=100,
        audit=None,
    ):
        if not has_exact_type(max_items, int) or not 0 < max_items <= 1000:
            raise ValueError("max_items 必须位于 [1, 1000]")
        self._api, self.sources = api, sources
        self._security_for, self._scope_for, self._authorize_use = (
            security_for,
            scope_for,
            authorize_use,
        )
        self._detector_version_for = detector_version_for
        self._protected_source_levels_for = protected_source_levels_for
        self._detector_version = inspector.version
        self._inspector = PayloadInspector(
            inspector, frozenset({"content", "abstract", "overview"})
        )
        self.provider, self.purpose, self.max_items = provider, purpose, max_items
        self.audit = audit
        self._secret = secrets.token_bytes(32)
        self._closed = False
        # 显式注册可信 SDK 校验器；不接受模型返回的等级或外部 JSON 自述来源。
        sources.register_provider(provider, self._validate_source)

    def _check_inspection_policy(self, actor):
        # Detector revision is plugin-owned; it does not assert SDK rule equivalence.
        version = (
            self._detector_version_for(actor)
            if self._detector_version_for is not None
            else self._detector_version
        )
        if (
            not has_exact_type(version, str)
            or not version
            or version != self._inspector.text.version
        ):
            raise SecurityDenied("插件检测版本已改变或无法确认")

    def _context(self, actor):
        from jiuwen_memory.api import RequestSecurityContext, Scope

        if self._closed or not isinstance(actor, Actor):
            raise SecurityDenied("记忆交付适配器已关闭或主体未知")
        self.sources.snapshot(actor)
        self._check_inspection_policy(actor)
        scope, security = self._scope_for(actor), self._security_for(actor)
        if not isinstance(scope, Scope) or not isinstance(security, RequestSecurityContext):
            raise SecurityDenied("SDK 身份或目标范围缺失")
        identity = security.auth.actor
        if (
            scope.org != actor.tenant_id
            or identity.org != actor.tenant_id
            or identity.user != actor.user_id
        ):
            raise SecurityDenied("SDK 身份与 agent 可信主体不匹配")
        elif identity.agent and identity.agent != actor.agent_id:
            raise SecurityDenied("SDK 身份与 agent 可信主体不匹配")
        elif identity.session and identity.session != actor.session_id:
            raise SecurityDenied("SDK 身份与 agent 可信主体不匹配")
        return replace(scope), security

    @staticmethod
    def _coordinates(scope):
        return tuple(getattr(scope, key) for key in ("org", "space", "user", "agent", "session"))

    def _reference(self, unit, actor, requested):
        from jiuwen_memory.api import LifecycleState, MemoryUnit, Modality

        self._check_inspection_policy(actor)

        if not has_exact_type(unit, MemoryUnit) or not unit.id:
            raise SecurityDenied("不支持的 SDK 记忆结果")
        if unit.lifecycle != LifecycleState.ACTIVE:
            raise SecurityDenied("记忆来源已失效、归档或被替代")
        coordinates, bounds = self._coordinates(unit.scope), self._coordinates(requested)
        if coordinates[0] != actor.tenant_id or any(
            bound and actual != bound for actual, bound in zip(coordinates, bounds, strict=True)
        ):
            raise SecurityDenied("SDK 返回的记忆超出请求范围")
        if unit.assets or any(
            s.source not in {Modality.TEXT, Modality.CODE} for s in unit.segments
        ):
            raise SecurityDenied("尚未支持附件或多模态记忆交付")
        level = unit.system_metadata.get("privacy_level")
        revision = unit.system_metadata.get("privacy_revision")
        if (
            level not in {"S1", "S2", "S3"}
            or not has_exact_type(revision, str)
            or len(revision) != 32
        ):
            raise SecurityDenied("SDK 记忆缺少可信隐私等级或修订")
        elif any(c not in "0123456789abcdef" for c in revision):
            raise SecurityDenied("SDK 记忆缺少可信隐私等级或修订")
        protected = self._protected_source_levels_for(actor)
        if not isinstance(protected, (set, frozenset)) or protected - {"S2", "S3"}:
            raise SecurityDenied("SDK 敏感来源未确认提供脱敏视图")
        elif level != "S1" and level not in protected:
            raise SecurityDenied("SDK 敏感来源未确认提供脱敏视图")
        digest = hmac.new(
            self._secret,
            encode_payload([unit.content, unit.layers.l0, unit.layers.l1]).encode(),
            hashlib.sha256,
        ).hexdigest()
        return SourceReference(
            self.provider,
            coordinates,
            unit.id,
            revision,
            level,
            self.purpose,
            self._inspector.version,
            digest,
        )

    def _read_unit(self, actor, unit_id, *, scope=None):
        requested, security = self._context(actor)
        unit = self._api.get(unit_id, scope or requested, security=security)
        if getattr(unit, "id", None) != unit_id:
            raise SecurityDenied("SDK 请求与实际记忆标识不一致")
        return unit, requested

    def _validate_source(self, actor, reference):
        from jiuwen_memory.api import Scope

        if reference.provider != self.provider or reference.purpose != self.purpose:
            return False
        if self._authorize_use(actor, reference, self.purpose) is not True:
            return False
        org, space, user, agent, session = reference.scope
        unit, requested = self._read_unit(
            actor,
            reference.unit_id,
            scope=Scope(org=org, space=space, user=user, agent=agent, session=session),
        )
        current = self._reference(unit, actor, requested)
        return (
            current.unit_id == reference.unit_id
            and current.scope == reference.scope
            and current.revision == reference.revision
            and current.presentation_version == reference.presentation_version
            and current.snapshot_digest == reference.snapshot_digest
            and strongest(current.level, reference.level) == reference.level
        )

    def _deliver(self, actor, units, requested):
        if not has_exact_type(units, list) or len(units) > self.max_items:
            raise SecurityDenied("SDK 返回条数超出交付预算")
        views, references = [], []
        for unit in units:
            reference = self._reference(unit, actor, requested)
            if self._authorize_use(actor, reference, self.purpose) is not True:
                raise SecurityDenied("agent 无权使用此记忆")
            payload = {
                "content": unit.content,
                "abstract": unit.layers.l0,
                "overview": unit.layers.l1,
            }
            result = self._inspector.inspect(payload)
            if result.redacted_json is None:
                raise SecurityDenied("无法构造可交付的记忆脱敏视图")
            safe = json.loads(result.redacted_json)
            verified = self._inspector.inspect(safe)
            if verified.level != "S1" or verified.redacted_json != result.redacted_json:
                raise SecurityDenied("记忆脱敏视图复检失败")
            # 下层写入豁免不能扩大 agent 使用权；独立检测只允许提高来源等级。
            reference = SourceReference(
                reference.provider,
                reference.scope,
                reference.unit_id,
                reference.revision,
                strongest(reference.level, result.level),
                reference.purpose,
                reference.presentation_version,
                reference.snapshot_digest,
            )
            token = hmac.new(
                self._secret,
                encode_payload(
                    [
                        vars(actor),
                        reference.provider,
                        list(reference.scope),
                        reference.unit_id,
                        reference.revision,
                        reference.presentation_version,
                    ]
                ).encode(),
                hashlib.sha256,
            ).hexdigest()
            references.append(reference)
            views.append(MemoryView("mem_" + token, **safe))
        # 原子登记必须先于返回。任何一条失败，整批视图均不交付。
        snapshot = self.sources.record(actor, references)
        if self.audit is not None:
            from .hooks import Hook

            self.audit.emit(
                actor=actor,
                hook=Hook.BEFORE_MEMORY_EXPORT,
                operation_id=secrets.token_urlsafe(24),
                target=self.provider,
                level=snapshot.level,
                outcome="released",
            )
        return tuple(views)

    def get(self, unit_id, actor: Actor) -> MemoryView:
        try:
            unit, scope = self._read_unit(actor, unit_id)
            return self._deliver(actor, [unit], scope)[0]
        except Exception:
            raise SecurityDenied("记忆读取或安全交付失败") from None

    def list(self, actor: Actor, *, limit=50) -> tuple[MemoryView, ...]:
        from jiuwen_memory.api import MemoryListResult

        if not has_exact_type(limit, int) or not 0 < limit <= self.max_items:
            raise SecurityDenied("记忆分页预算无效")
        try:
            scope, security = self._context(actor)
            result = self._api.list(scope, security=security, limit=limit)
            if not has_exact_type(result, MemoryListResult):
                raise SecurityDenied("不支持的 SDK 列表结果")
            return self._deliver(actor, result.items, scope)
        except Exception:
            raise SecurityDenied("记忆列表或安全交付失败") from None

    def search(self, query, actor: Actor, *, top_k=10) -> tuple[MemoryView, ...]:
        from jiuwen_memory.api import Context, DisclosureLevel, RetrievalResult

        if (
            not has_exact_type(query, str)
            or not has_exact_type(top_k, int)
            or not 0 < top_k <= self.max_items
        ):
            raise SecurityDenied("记忆查询参数或预算无效")
        try:
            scope, security = self._context(actor)
            result = self._api.search(
                query,
                Context(scope=scope),
                security=security,
                top_k=top_k,
                disclosure=DisclosureLevel.L2,
            )
            if (
                not has_exact_type(result, RetrievalResult)
                or result.errors
                or len(result.items) > top_k
            ):
                raise SecurityDenied("不支持聚合视图、未知来源或部分失败的检索结果")
            units = []
            for item in result.items:
                unit, _ = self._read_unit(actor, item.unit_id)
                reference = self._reference(unit, actor, scope)
                if (
                    item.system_metadata.get("privacy_revision") != reference.revision
                    or item.system_metadata.get("privacy_level") != reference.level
                    or item.content != unit.content
                ):
                    raise SecurityDenied("检索与读取的来源快照不一致")
                units.append(unit)
            return self._deliver(actor, units, scope)
        except Exception:
            raise SecurityDenied("记忆检索或安全交付失败") from None

    async def aget(self, unit_id, actor: Actor) -> MemoryView:
        return await asyncio.to_thread(self.get, unit_id, actor)

    async def alist(self, actor: Actor, *, limit=50) -> tuple[MemoryView, ...]:
        return await asyncio.to_thread(self.list, actor, limit=limit)

    async def asearch(self, query, actor: Actor, *, top_k=10) -> tuple[MemoryView, ...]:
        return await asyncio.to_thread(self.search, query, actor, top_k=top_k)

    def close(self):
        # 不关闭宿主借用的 MemoryAPI 或共享账本；撤销此提供方后续使用。
        self._closed = True
        self.sources.unregister_provider(self.provider, self._validate_source)
