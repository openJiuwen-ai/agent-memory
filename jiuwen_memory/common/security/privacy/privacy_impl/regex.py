"""Regex policies and volatile opaque staging, backed by an encrypted PrivacyStore.

Private state is never an ordinary recall source. An
authenticated manifest binds each disclosure to the full Scope, unit ID, revision
and current protected textual projections. Staging never writes ingest input to disk.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.security.privacy.base import (
    PRIVACY_TOKEN as TOKEN,
)
from jiuwen_memory.common.security.privacy.base import (
    HumanViewContent,
    PrivacyProducer,
    PrivacyProtection,
)
from jiuwen_memory.common.security.privacy.snapshots import DisclosureSnapshots
from jiuwen_memory.common.type_def import (
    AuditEvent,
    LifecycleState,
    MemoryUnit,
    Modality,
    RawPayload,
    Scope,
)
from jiuwen_memory.common.type_def.memory import (
    KERNEL_SYSTEM_METADATA_KEYS,
    TRANSIENT_SYSTEM_METADATA_KEYS,
)
from jiuwen_memory.storage.privacy import (
    FragmentReference,
    PrivacySnapshot,
    PrivacyStore,
    PrivacyStoreProducer,
    encrypted_sqlite_privacy_store,
)

if TYPE_CHECKING:
    from jiuwen_memory.common.security.cryptography import CryptographyProvider

from .policy import DictPrivacyManager, SensitiveTaxonomy
from .static_privacy_classifier import StaticPrivacyClassifier, outside_tokens

LEVEL_KEY = "privacy_level"
REVISION_KEY = "privacy_revision"
MESSAGE_REVISION_KEY = "privacy_message_revision"
RULES_KEY = "privacy_rule_ids"
CATEGORIES_KEY = "privacy_categories"
_RESERVED = {LEVEL_KEY, REVISION_KEY, MESSAGE_REVISION_KEY, RULES_KEY, CATEGORIES_KEY}
_MATCH_BUDGET: ContextVar[tuple[object, float] | None] = ContextVar(
    "privacy_match_budget", default=None
)


@dataclass
class _StageCapture:
    provider: object
    parent: _StageCapture | None = None
    tokens: set[tuple[str, str]] = field(default_factory=set)
    closed: bool = False


_STAGING: ContextVar[_StageCapture | None] = ContextVar("privacy_staging_scope", default=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scope(scope: Scope) -> str:
    return _json(asdict(scope))


def _evidence(metadata: dict) -> dict[str, set[str]]:
    return {
        key: {value for value in metadata.get(key, []) if isinstance(value, str)}
        if isinstance(metadata.get(key, []), list) else set()
        for key in (RULES_KEY, CATEGORIES_KEY)
    }


def _record_evidence(metadata: dict, evidence: dict[str, set[str]]) -> None:
    for key, values in evidence.items():
        metadata[key] = sorted(values)[:64]


def _walk(value: Any, transform: Callable[..., str], *, label: str = "") -> Any:
    if isinstance(value, str):
        return transform(value, label=label)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        protected = transform(str(value), label=label)
        return protected if protected != str(value) else value
    if isinstance(value, list):
        return [_walk(item, transform, label=label) for item in value]
    if isinstance(value, dict):
        # Metadata names can themselves contain PII. Preserve technical names
        # only when they are safe under the same classifier.
        return {
            transform(str(key)): _walk(item, transform, label=str(key))
            for key, item in value.items()
        }
    return value


def _texts(unit: MemoryUnit) -> dict[str, Any]:
    return {
        "segments": [segment.content for segment in unit.segments],
        "layers": asdict(unit.layers),
        "tags": unit.tags,
        "entities": unit.entities,
        "user_metadata": unit.user_metadata,
        "system_metadata": {
            key: value
            for key, value in unit.system_metadata.items()
            if key not in _RESERVED and isinstance(value, (str, int, float, bool, list, type(None)))
        },
        "facts": [fact.text for fact in unit.tree_memory.facts] if unit.tree_memory else [],
    }


def _digest(unit: MemoryUnit) -> str:
    # Index builders add technical system metadata. It is protected separately,
    # but is not part of the human sentence or its version fingerprint.
    values = _texts(unit)
    values.pop("system_metadata")
    return hashlib.sha256(_json(values).encode()).hexdigest()


class RegexPrivacy(PrivacyProtection):
    @contextmanager
    def _matching_scope(self):
        current = _MATCH_BUDGET.get()
        if current is not None and current[0] is self:
            yield
            return
        token = _MATCH_BUDGET.set((self, time.monotonic() + self._regex_timeout))
        try:
            with self._capture_stage(always=False):
                yield
        finally:
            _MATCH_BUDGET.reset(token)

    def __init__(
        self,
        crypto: CryptographyProvider | None = None,
        path: str = "",
        *,
        store: PrivacyStore | None = None,
        levels=None,
        rules=None,
        use_builtin_rules: bool | None = None,
        category_levels: dict[str, str] | None = None,
        rule_overrides: dict[str, dict] | None = None,
        staging_ttl: int = 300,
        staging_limit: int = 10000,
        regex_timeout: float = 2.0,
    ) -> None:
        try:
            import regex as bounded_regex
        except ImportError as exc:
            raise ValidationError(
                "privacy regex requires pip install JiuwenMemory[security]"
            ) from exc
        try:
            taxonomy = SensitiveTaxonomy(levels)
            self.levels = taxonomy.levels
            self._classifier = StaticPrivacyClassifier(
                taxonomy, bounded_regex, rules=rules, use_builtin_rules=use_builtin_rules,
                category_levels=category_levels, rule_overrides=rule_overrides,
            )
            self._policy = DictPrivacyManager(taxonomy, self._classifier)
            self.rules = self._classifier.rules
            if staging_ttl <= 0 or staging_limit <= 0:
                raise ValueError
            if not isinstance(regex_timeout, (int, float)) or isinstance(regex_timeout, bool):
                raise ValueError
            if not 0 < regex_timeout <= 30:
                raise ValueError
        except (TypeError, KeyError, ValueError, bounded_regex.error) as exc:
            raise ValidationError("invalid privacy levels or regex rules") from exc
        self._regex_timeout = float(regex_timeout)
        self._owns_store = store is None
        self._store = store if store is not None else encrypted_sqlite_privacy_store(crypto, path)
        if not self._store.security.enabled():
            raise ValidationError("privacy requires an encrypted PrivacyStore")
        self._audit = None
        try:
            self._store.health()
        except BaseException:
            if self._owns_store:
                self._store.close()
            raise
        self._ttl, self._limit = staging_ttl, staging_limit
        self._pending: dict[tuple[str, str], tuple[float, FragmentReference, str]] = {}
        self._disclosures = DisclosureSnapshots()
        self._lock = threading.RLock()

    def security_dependencies(self) -> tuple:
        if self._owns_store:
            # Legacy direct construction owns its private Store, but borrows the
            # same cryptography dependency as before. Do not close it twice.
            return self._store.security_dependencies()
        return (self._store,)

    def bind_audit(self, audit) -> None:
        self._audit = audit

    @contextmanager
    def _capture_stage(self, *, always: bool):
        parent = _STAGING.get()
        captured = _StageCapture(self, parent if parent and parent.provider is self else None)
        token = _STAGING.set(captured)
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            with self._lock:
                captured.closed = True
                if failed or always:
                    for key in captured.tokens:
                        self._pending.pop(key, None)
            _STAGING.reset(token)

    @contextmanager
    def staging_scope(self, *, retain_on_success: bool = False):
        with self._capture_stage(always=not retain_on_success):
            yield

    def close(self) -> None:
        with self._lock:
            self._disclosures.clear()
            self._pending.clear()
            if self._owns_store:
                self._store.close()

    def health(self) -> None:
        self._store.health()

    def _matches(
        self, text: str, *, evidence: dict[str, set[str]] | None = None
    ) -> list[tuple[int, int, str]]:
        budget = _MATCH_BUDGET.get()
        deadline = (budget[1] if budget and budget[0] is self
                    else time.monotonic() + self._regex_timeout)
        hits = self._classifier.detect(
            text, deadline=deadline, capacity=self._limit,
            protected=[match.span() for match in TOKEN.finditer(text)],
        )
        mark = self._policy.classify(hits)
        if evidence is not None:
            evidence[RULES_KEY].update(mark.matched_rules)
            evidence[CATEGORIES_KEY].update(mark.categories)
        return [(hit.start, hit.end, hit.level) for hit in self._classifier.merge(hits)]

    def _mask(
        self, text: str, scope: Scope | None, detected: set[str], owner: str = "", label: str = "",
        evidence: dict[str, set[str]] | None = None,
    ) -> str:
        result, cursor = [], 0
        if "[JW-PRIVATE:" in TOKEN.sub("", text):
            raise ValidationError("malformed privacy placeholder")
        offset = len(label) + 1 if label else 0
        labeled = label + "=" + text if label else text
        matches = [
            (max(0, start - offset), end - offset, level)
            for start, end, level in self._matches(labeled, evidence=evidence)
            if end > offset
        ]
        plain_matches = set(self._matches(text, evidence=evidence)) if label else set()
        if label and any(match not in plain_matches for match in matches):
            # A credential field protects its entire value, including whitespace;
            # a normal note containing a phone number still masks only that span.
            strongest = self._strongest({item[2] for item in matches})
            protected = [match.span() for match in TOKEN.finditer(text)]
            intervals = outside_tokens(0, len(text), protected, [span[0] for span in protected])
            matches = [(start, end, strongest) for start, end in intervals]
        for start, end, level in matches:
            detected.add(level)
            if self._policy.decide_write(level).action == "allow":
                continue
            result.append(text[cursor:start])
            if scope is None:
                result.append("[PRIVATE]")
            else:
                token = "[JW-PRIVATE:" + secrets.token_hex(24) + "]"
                fragment = self._store.stage_fragment(scope, token, text[start:end].encode(), level)
                with self._lock:
                    now = time.monotonic()
                    self._pending = {
                        key: value
                        for key, value in self._pending.items()
                        if now - value[0] <= self._ttl
                    }
                    if len(self._pending) >= self._limit:
                        raise ValidationError("privacy staging capacity exceeded")
                    namespace = _scope(scope)
                    captured = _STAGING.get()
                    while captured is not None and captured.provider is self:
                        if captured.closed:
                            raise ValidationError("privacy staging operation is closed")
                        captured.tokens.add((namespace, token))
                        captured = captured.parent
                    self._pending[(namespace, token)] = (now, fragment, owner)
                result.append(token)
            cursor = end
        result.append(text[cursor:])
        return "".join(result)

    def preview(self, text: str, *, label: str = "") -> str:
        with self._matching_scope():
            if "[JW-PRIVATE:" in text:
                raise ValidationError("privacy placeholders are reserved for server use")
            return self._mask(text, None, set(), label=label)

    def preview_echo(self, value: Any, *, label: str = "") -> Any:
        with self._matching_scope():
            return super().preview_echo(value, label=label)

    def ingress(self, payload: RawPayload) -> RawPayload:
        with self._matching_scope():
            if (
                payload.modality not in {Modality.TEXT, Modality.CODE}
                or payload.assets
                or payload.uri
            ):
                raise ValidationError("privacy currently supports inline TEXT/CODE without assets")
            result = copy.copy(payload)
            result.id = payload.id or secrets.token_hex(16)
            detected: set[str] = set()
            evidence = _evidence({})

            def inspect(text: str, *, label: str = "") -> str:
                if "[JW-PRIVATE:" in text:
                    raise ValidationError("privacy placeholders are reserved for server use")
                # Classification is retained, but trusted processing receives the
                # original text. Ciphertext staging starts at the persistence boundary.
                labeled = label + "=" + text if label else text
                detected.update(level for _, _, level in self._matches(labeled, evidence=evidence))
                return text

            result.data = inspect(payload.data.decode("utf-8")).encode("utf-8")
            result.user_metadata = _walk(result.user_metadata, inspect)
            result.system_metadata = {
                key: value
                if key in KERNEL_SYSTEM_METADATA_KEYS | TRANSIENT_SYSTEM_METADATA_KEYS
                else _walk(value, inspect, label=key)
                for key, value in result.system_metadata.items()
            }
            result.system_metadata[LEVEL_KEY] = self._strongest(detected)
            _record_evidence(result.system_metadata, evidence)
            return result

    def split_input(self, text: str, limit: int) -> list[str]:
        if limit <= 0 or len(text) <= limit:
            return [text]
        with self._matching_scope():
            spans = [(start, end) for start, end, level in self._matches(text)
                     if self.levels[level].write == "redact"]
        spans.extend(match.span() for match in TOKEN.finditer(text))
        spans.sort()
        pieces, offset = [], 0
        while offset < len(text):
            end = min(offset + limit, len(text))
            for start, stop in spans:
                if start < end < stop:
                    end = start
                    break
            if end <= offset:
                raise ValidationError("sensitive span exceeds configured text limit")
            pieces.append(text[offset:end])
            offset = end
        return pieces

    def _strongest(self, names: set[str]) -> str:
        return self._policy.taxonomy.max_level(names)

    def inherit_classification(self, unit, sources) -> None:
        names = {
            source.system_metadata.get(LEVEL_KEY)
            for source in sources
            if source.scope == unit.scope
        }
        names.add(unit.system_metadata.get(LEVEL_KEY))
        unit.system_metadata[LEVEL_KEY] = self._policy.inherit_sensitivity(
            names & self.levels.keys()
        )
        evidence = _evidence(unit.system_metadata)
        for source in sources:
            if source.scope == unit.scope:
                for key, values in _evidence(source.system_metadata).items():
                    evidence[key].update(values)
        _record_evidence(unit.system_metadata, evidence)

    def prepare(self, unit: MemoryUnit) -> None:
        with self._matching_scope():
            if unit.assets or any(
                segment.source not in {Modality.TEXT, Modality.CODE} for segment in unit.segments
            ):
                raise ValidationError("privacy currently supports TEXT/CODE without assets")
            before_texts = _json(_texts(unit))
            detected: set[str] = set()
            evidence = _evidence(unit.system_metadata)
            previous = unit.system_metadata.get(LEVEL_KEY)
            if previous in self.levels:
                detected.add(previous)

            def protect(text: str, *, label: str = "") -> str:
                return self._mask(text, unit.scope, detected, unit.id, label, evidence)

            for segment in unit.segments:
                segment.content = protect(segment.content)
            unit.layers.l0, unit.layers.l1 = protect(unit.layers.l0), protect(unit.layers.l1)
            unit.tags = _walk(unit.tags, protect)
            unit.entities = _walk(unit.entities, protect)
            unit.user_metadata = _walk(unit.user_metadata, protect)
            unit.system_metadata = {
                key: value
                if key in KERNEL_SYSTEM_METADATA_KEYS | TRANSIENT_SYSTEM_METADATA_KEYS
                else _walk(value, protect, label=key)
                for key, value in unit.system_metadata.items()
            }
            if unit.tree_memory:
                for fact in unit.tree_memory.facts:
                    fact.text = protect(fact.text)
            # Original level is retained even if Normalizer/LLM drops its placeholder.
            unit.system_metadata[LEVEL_KEY] = self._strongest(detected)
            _record_evidence(unit.system_metadata, evidence)
            if before_texts != _json(_texts(unit)):
                unit.vectors.clear()
            # Index-only builders must reject foreign references too, without
            # publishing a manifest or replacing a truth snapshot.
            with self._lock:
                references = {}
                for token in {match.group() for match in TOKEN.finditer(_json(_texts(unit)))}:
                    self._fragment(unit, token, references)

    def _fragment(self, unit: MemoryUnit, token: str, references: dict) -> FragmentReference:
        namespace = _scope(unit.scope)
        owners = {unit.id, unit.source_ref, unit.supersedes, *unit.provenance} - {""}
        staged = self._pending.get((namespace, token))
        if staged and time.monotonic() - staged[0] <= self._ttl and staged[2] in owners:
            return staged[1]
        sources = [prefix + "/" + uid for uid in owners for prefix in ("memory", "message")]
        if "tokens" not in references:
            references["tokens"] = set()
            for snapshot in self._store.search(unit.scope, sources):
                # Manifest reads authenticate metadata, never sensitive fragment values.
                manifest = json.loads(snapshot.value)
                if manifest["digest"] != snapshot.digest:
                    raise ValidationError("privacy reference manifest mismatch")
                references["tokens"].update(manifest["tokens"])
        if token in references["tokens"]:
            try:
                return self._store.reference(unit.scope, token)
            except NotFoundError:
                pass
        raise ValidationError("unbound or expired privacy placeholder")

    def relocate(self, unit: MemoryUnit, target: Scope) -> None:
        if target == unit.scope:
            return
        if target.org != unit.scope.org:
            raise ValidationError("privacy routing cannot cross organizations")
        tokens = {match.group() for match in TOKEN.finditer(_json(_texts(unit)))}
        with self._lock:
            now = time.monotonic()
            pending = {
                key: value for key, value in self._pending.items() if now - value[0] <= self._ttl
            }
            if len(pending) + len(tokens) > self._limit:
                raise ValidationError("privacy staging capacity exceeded")
            references = {}
            fragments = [(token, self._fragment(unit, token, references)) for token in tokens]
            for token, fragment in fragments:
                pending[(_scope(target), token)] = (now, fragment, unit.id)
                captured = _STAGING.get()
                while captured is not None and captured.provider is self:
                    if captured.closed:
                        raise ValidationError("privacy staging operation is closed")
                    captured.tokens.add((_scope(target), token))
                    captured = captured.parent
            self._pending = pending

    def validate_existing(self, unit: MemoryUnit, *, channel: str = "memory") -> None:
        key = REVISION_KEY if channel == "memory" else MESSAGE_REVISION_KEY
        revision = unit.system_metadata.get(key)
        if (
            not isinstance(revision, str)
            or len(revision) != 32
            or any(ch not in "0123456789abcdef" for ch in revision)
        ):
            raise ValidationError("privacy requires migration of existing unprotected memory")
        with self.staging_scope():
            protected = copy.deepcopy(unit)
            self.prepare(protected)
            if _texts(protected) != _texts(unit):
                raise ValidationError("privacy policy changed; existing memory requires migration")

    def persist(self, unit: MemoryUnit, *, channel: str = "memory") -> None:
        if channel not in {"memory", "message"}:
            raise ValidationError("invalid privacy persistence channel")
        self.prepare(unit)
        digest = _digest(unit)
        record_id = channel + "/" + unit.id
        tokens = sorted({match.group() for match in TOKEN.finditer(_json(_texts(unit)))})
        if "[JW-PRIVATE:" in TOKEN.sub("", _json(_texts(unit))):
            raise ValidationError("malformed privacy placeholder")
        revision = secrets.token_hex(16)
        with self._lock, self._store.transaction(unit.scope):
            try:
                existing = self._store.get(unit.scope, record_id)
            except NotFoundError:
                existing = None
            if existing and existing.digest == digest:
                authenticated = json.loads(existing.value)
                if authenticated["digest"] != digest:
                    raise ValidationError("privacy reference manifest mismatch")
                if authenticated["tokens"] == tokens:
                    key = REVISION_KEY if channel == "memory" else MESSAGE_REVISION_KEY
                    unit.system_metadata[key] = existing.revision
                    levels = {self._store.reference(unit.scope, token).level for token in tokens}
                    unit.system_metadata[LEVEL_KEY] = self._strongest(
                        levels | {unit.system_metadata[LEVEL_KEY]}
                    )
                    return
            references = {}
            fragments = {token: self._fragment(unit, token, references) for token in tokens}
            positions = {token: [] for token in tokens}
            for index, segment in enumerate(unit.segments):
                for match in TOKEN.finditer(segment.content):
                    positions[match.group()].append(
                        {
                            "index": index,
                            "start": match.start(),
                            "end": match.end(),
                        }
                    )
            manifest = PrivacySnapshot(
                record_id, revision, digest,
                _json({"tokens": tokens, "digest": digest, "segments": positions}).encode(),
            )
            if existing is None:
                self._store.insert(unit.scope, manifest, fragments)
            else:
                self._store.update(unit.scope, manifest, fragments)
            key = REVISION_KEY if channel == "memory" else MESSAGE_REVISION_KEY
            unit.system_metadata[key] = revision
            unit.system_metadata[LEVEL_KEY] = self._strongest(
                {unit.system_metadata[LEVEL_KEY]}
                | {fragment.level for fragment in fragments.values()}
            )
            if self._audit is not None:
                self._audit.record(
                    AuditEvent(
                        actor=Scope(org=unit.scope.org, agent="privacy-processor"),
                        target=unit.scope,
                        target_id=unit.id,
                        action="privacy_protection",
                        layer="security",
                        decision="allow",
                        occurred_at=datetime.now(UTC),
                        detail={
                            "privacy_level": str(unit.system_metadata[LEVEL_KEY]),
                            "fragment_count": str(len(tokens)),
                            "channel": channel,
                            "rule_ids": _json(unit.system_metadata.get(RULES_KEY, [])),
                            "categories": _json(unit.system_metadata.get(CATEGORIES_KEY, [])),
                        },
                    )
                )

    def refresh_revision(self, unit: MemoryUnit) -> None:
        with self._lock:
            try:
                snapshot = self._store.get(unit.scope, "memory/" + unit.id)
            except NotFoundError:
                return
            if snapshot.digest == _digest(unit):
                unit.system_metadata[REVISION_KEY] = snapshot.revision

    def write(
        self,
        unit: MemoryUnit,
        commit: Callable[[MemoryUnit], None],
        read: Callable[[], MemoryUnit | None],
        *,
        channel: str = "memory",
    ) -> None:
        """Keep encrypted recovery bindings until the truth write's outcome is known.

        The lock serializes this provider's truth mutations and private collection.
        Recovery rows are durable: an unavailable read-back must not destroy either
        candidate. Human disclosure still loads current truth and checks its revision.
        """
        record_id = channel + "/" + unit.id
        with self._lock:
            self._store.checkpoint(unit.scope, record_id)
            try:
                self.persist(unit, channel=channel)
                commit(unit)
            except BaseException:
                try:
                    current = read()
                    if current is None:
                        self.discard(unit.scope, [unit.id], channel=channel)
                    elif current.scope == unit.scope and current.id == unit.id:
                        self._reconcile(current, channel=channel)
                except BaseException:
                    # Preserve both encrypted candidates if truth cannot be read
                    # or reconciliation fails. Keep the original write exception.
                    pass
                raise
            else:
                self._store.confirm(unit.scope, record_id)

    def _reconcile(self, unit: MemoryUnit, *, channel: str) -> None:
        key = REVISION_KEY if channel == "memory" else MESSAGE_REVISION_KEY
        self._store.reconcile(
            unit.scope, channel + "/" + unit.id, unit.system_metadata.get(key, ""), _digest(unit)
        )

    def discard(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        channel: str = "memory",
        commit: Callable[[], None] | None = None,
    ) -> None:
        unit_ids = [channel + "/" + uid for uid in unit_ids]
        with self._lock:
            if commit is not None:
                commit()
            self._discard_records(scope, unit_ids)

    def _discard_records(self, scope: Scope, unit_ids: list[str]) -> None:
        for token in self._store.delete(scope, unit_ids):
            self._pending.pop((_scope(scope), token), None)

    def discard_scope(self, scope: Scope) -> None:
        selected = {key: value for key, value in asdict(scope).items() if value}
        with self._lock:
            namespaces = {_scope(actual) for actual in self._store.scopes()}
            namespaces.update(key[0] for key in self._pending)
            for namespace in namespaces:
                actual = json.loads(namespace)
                if all(actual[key] == value for key, value in selected.items()):
                    actual_scope = Scope(**actual)
                    self._store.delete(actual_scope, None)
                    self._pending = {
                        key: value for key, value in self._pending.items() if key[0] != namespace
                    }

    def stage_disclosure(self, unit: MemoryUnit) -> str:
        with self._lock:
            if unit.lifecycle is not LifecycleState.ACTIVE:
                raise ValidationError("memory is not active; request a current snapshot")
            self.validate_existing(unit)
            return self._disclosures.stage(unit)

    def cancel_disclosure(self, operation: str) -> None:
        self._disclosures.discard(operation)

    def render_disclosure(self, operation: str, current: MemoryUnit) -> HumanViewContent:
        with self._lock:
            snapshot = self._disclosures.consume(operation)
            if current.lifecycle is not LifecycleState.ACTIVE:
                raise ValidationError("memory is not active; request a current snapshot")
            if current.id != snapshot.id or current.scope != snapshot.scope:
                raise ValidationError("memory changed; request a new human verification")
            revision = current.system_metadata.get(REVISION_KEY)
            if revision != snapshot.system_metadata.get(REVISION_KEY):
                raise ValidationError("memory changed; request a new human verification")
            if _digest(current) != _digest(snapshot):
                raise ValidationError("memory changed; request a new human verification")
            return HumanViewContent(snapshot.id, self.render(snapshot))

    def render(self, unit: MemoryUnit) -> str:
        # The human disclosure boundary (including its dedicated engine return
        # path) owns proof/PEP checks. Ordinary query paths never call this method.
        with self._lock, self._store.transaction(unit.scope):
            rows = self._store.search(
                unit.scope, ["memory/" + unit.id],
                revision=unit.system_metadata.get(REVISION_KEY, ""),
            )
            if not rows or rows[0].digest != _digest(unit):
                raise ValidationError("privacy snapshot is no longer available")
            manifest = json.loads(rows[0].value)
            if manifest["digest"] != _digest(unit):
                raise ValidationError("privacy snapshot mismatch")
            values = {}
            for token in manifest["tokens"]:
                try:
                    fragment = self._store.reference(unit.scope, token)
                except NotFoundError as exc:
                    raise ValidationError("privacy fragment is no longer available") from exc
                if fragment.level not in self.levels:
                    raise ValidationError("privacy fragment is no longer available")
                if self.levels[fragment.level].reveal:
                    values[token] = self._store.get_fragment(unit.scope, token).decode("utf-8")
            return TOKEN.sub(lambda match: values.get(match.group(), match.group()), unit.content)


@PrivacyProducer.register("regex")
def _build(config):
    if config.get("llm_enabled", False):
        raise ValidationError("privacy LLM classification is not implemented")
    return RegexPrivacy(
        store=PrivacyStoreProducer.for_privacy(config),
        levels=config.get("levels"),
        rules=config.get("rules"),
        use_builtin_rules=config.get("use_builtin_rules"),
        category_levels=config.get("category_levels"),
        rule_overrides=config.get("rule_overrides"),
        staging_ttl=int(config.get("staging_ttl", 300)),
        staging_limit=int(config.get("staging_limit", 10000)),
        regex_timeout=config.get("regex_timeout", 2.0),
    )
