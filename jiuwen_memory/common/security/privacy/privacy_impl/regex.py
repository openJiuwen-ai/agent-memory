"""Regex policies, volatile encrypted staging and an isolated encrypted fragment vault.

SQLite is private security state, never a StoreManager port or a recall source. An
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
from bisect import bisect_right
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security._sqlite_state import open_private_state
from jiuwen_memory.common.security.cryptography.base import (
    CryptographyProducer,
    CryptographyProvider,
)
from jiuwen_memory.common.security.privacy.base import (
    PRIVACY_TOKEN as TOKEN,
)
from jiuwen_memory.common.security.privacy.base import PrivacyProducer, PrivacyProtection
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import AuditEvent, MemoryUnit, Modality, RawPayload, Scope
from jiuwen_memory.common.type_def.memory import (
    KERNEL_SYSTEM_METADATA_KEYS,
    TRANSIENT_SYSTEM_METADATA_KEYS,
)

LEVEL_KEY = "privacy_level"
REVISION_KEY = "privacy_revision"
MESSAGE_REVISION_KEY = "privacy_message_revision"
_RESERVED = {LEVEL_KEY, REVISION_KEY, MESSAGE_REVISION_KEY}
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


def _outside_tokens(
    start: int,
    end: int,
    protected: list[tuple[int, int]],
    starts: list[int],
) -> Iterator[tuple[int, int]]:
    """Subtract opaque references without suppressing adjacent newly generated secrets."""
    index = max(0, bisect_right(starts, start) - 1)
    cursor = start
    while index < len(protected) and protected[index][0] < end:
        left, right = protected[index]
        if cursor < left:
            yield cursor, min(left, end)
        cursor = max(cursor, right)
        index += 1
    if cursor < end:
        yield cursor, end


@dataclass(frozen=True)
class Level:
    name: str
    rank: int
    write: str = "redact"
    reveal: bool = True
    label: str = ""
    description: str = ""
    sync_mode: str = "none"  # full/none/index_only; enforcement is TODO
    retention_mode: str = "keep"  # keep/purge_after_ttl; enforcement is TODO
    retention_seconds: int = 0  # after soft deletion; enforcement is TODO
    share_mode: str = "no_share"  # shareable/internal_only/no_share; enforcement is TODO

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("invalid privacy level name")
        if not isinstance(self.label, str) or not isinstance(self.description, str):
            raise ValueError("invalid privacy level description")
        if not isinstance(self.reveal, bool):
            raise ValueError("privacy reveal must be bool")
        for value in (self.rank, self.retention_seconds):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("invalid privacy level integer")
        choices = (
            (self.write, {"allow", "redact"}),
            (self.sync_mode, {"full", "none", "index_only"}),
            (self.retention_mode, {"keep", "purge_after_ttl"}),
            (self.share_mode, {"shareable", "internal_only", "no_share"}),
        )
        for value, allowed in choices:
            if value not in allowed:
                raise ValueError("invalid privacy level policy")


@dataclass(frozen=True)
class Rule:
    level: str
    pattern: Any  # compiled regex supports a bounded finditer(timeout=...)
    group: str | int = 0


_DEFAULT_LEVELS = [
    {
        "name": "S1",
        "label": "公开",
        "rank": 1,
        "write": "allow",
        "sync_mode": "full",
        "share_mode": "shareable",
    },
    {"name": "S2", "label": "个人隐私", "rank": 2},
    {"name": "S3", "label": "凭据", "rank": 3},
]
_CREDENTIAL_LABEL = (
    r"(?i)(?:api[_ -]?key|(?:access|refresh|auth)[_ -]?token|password|passwd|"
    r"client[_ -]?secret|secret|private[_ -]?key|密码|密钥)[\"']?\s*[:=：]\s*"
)
_DEFAULT_RULES = [
    {
        "level": "S2",
        "pattern": (
            r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}"
            r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![A-Za-z0-9_])"
        ),
    },
    {"level": "S2", "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)"},
    {
        "level": "S2",
        "pattern": (
            r"(?i)(?<![a-z0-9._%+-])[a-z0-9._%+-]+@"
            r"[a-z0-9.-]+\.[a-z]{2,}(?![a-z0-9-])"
        ),
    },
    {
        "level": "S3",
        "pattern": _CREDENTIAL_LABEL + r"[\"']?(?P<value>[^\s\"',;，；}]{1,})",
        "group": "value",
    },
    {
        "level": "S3",
        "pattern": _CREDENTIAL_LABEL + r'"(?P<value>(?:\\.|[^"\\])+)"',
        "group": "value",
    },
    {
        "level": "S3",
        "pattern": _CREDENTIAL_LABEL + r"'(?P<value>(?:\\.|[^'\\])+)'",
        "group": "value",
    },
    {
        "level": "S3",
        "pattern": (
            r"-----BEGIN (?P<kind>(?:RSA |EC |OPENSSH |ENCRYPTED )?"
            r"PRIVATE KEY)-----(?:(?!-----BEGIN |-----END )[\s\S])*-----END (?P=kind)-----"
        ),
    },
    {"level": "S3", "pattern": r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"},
    {
        "level": "S3",
        "pattern": r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._~+/-]{1,}=*)",
        "group": "value",
    },
]


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
        crypto: CryptographyProvider,
        path: str,
        *,
        levels=None,
        rules=None,
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
            level_list = [
                Level(**item) for item in (levels if levels is not None else _DEFAULT_LEVELS)
            ]
            self.levels = {item.name: item for item in level_list}
            if (
                not level_list
                or len(self.levels) != len(level_list)
                or len({item.rank for item in level_list}) != len(level_list)
            ):
                raise ValueError
            self.rules = [
                Rule(item["level"], bounded_regex.compile(item["pattern"]), item.get("group", 0))
                for item in (rules if rules is not None else _DEFAULT_RULES)
            ]
            for rule in self.rules:
                if not isinstance(rule.group, (str, int)) or isinstance(rule.group, bool):
                    raise ValueError
                if rule.level not in self.levels:
                    raise ValueError
                if isinstance(rule.group, str) and rule.group not in rule.pattern.groupindex:
                    raise ValueError
                if isinstance(rule.group, int) and not 0 <= rule.group <= rule.pattern.groups:
                    raise ValueError
            if staging_ttl <= 0 or staging_limit <= 0:
                raise ValueError
            if not isinstance(regex_timeout, (int, float)) or isinstance(regex_timeout, bool):
                raise ValueError
            if not 0 < regex_timeout <= 30:
                raise ValueError
        except (TypeError, KeyError, ValueError, bounded_regex.error) as exc:
            raise ValidationError("invalid privacy levels or regex rules") from exc
        self._regex_timeout = float(regex_timeout)
        self._crypto = crypto
        self._audit = None
        crypto.health()
        self._ttl, self._limit = staging_ttl, staging_limit
        self._pending: dict[tuple[str, str], tuple[float, bytes, str, str, str]] = {}
        self._lock = threading.RLock()
        self._db = open_private_state(
            path,
            """
            PRAGMA foreign_keys=ON;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS fragments(
                scope TEXT NOT NULL, token TEXT NOT NULL, cipher BLOB NOT NULL,
                level TEXT NOT NULL, origin TEXT NOT NULL, PRIMARY KEY(scope,token));
            CREATE TABLE IF NOT EXISTS manifests(
                scope TEXT NOT NULL, unit TEXT NOT NULL, revision TEXT NOT NULL,
                digest TEXT NOT NULL, cipher BLOB NOT NULL, PRIMARY KEY(scope,unit));
            CREATE TABLE IF NOT EXISTS bindings(
                scope TEXT NOT NULL, unit TEXT NOT NULL, token TEXT NOT NULL,
                PRIMARY KEY(scope,unit,token),
                FOREIGN KEY(scope,unit) REFERENCES manifests(scope,unit) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS bindings_token ON bindings(scope,token);
            CREATE TABLE IF NOT EXISTS recovery_manifests(
                scope TEXT NOT NULL, unit TEXT NOT NULL, revision TEXT NOT NULL,
                digest TEXT NOT NULL, cipher BLOB NOT NULL, PRIMARY KEY(scope,unit,revision));
            CREATE TABLE IF NOT EXISTS recovery_bindings(
                scope TEXT NOT NULL, unit TEXT NOT NULL,
                revision TEXT NOT NULL, token TEXT NOT NULL,
                PRIMARY KEY(scope,unit,revision,token),
                FOREIGN KEY(scope,unit,revision)
                    REFERENCES recovery_manifests(scope,unit,revision) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS recovery_token ON recovery_bindings(scope,token);
        """,
        )

    def security_dependencies(self) -> tuple:
        return (self._crypto,)

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
            self._pending.clear()
            self._db.close()

    def health(self) -> None:
        with self._lock:
            self._db.execute("SELECT 1")
        self._crypto.health()

    def _context(
        self, scope: Scope, object_id: str, purpose: str, level: str = ""
    ) -> CryptoContext:
        return CryptoContext(
            scope=scope,
            purpose=purpose,
            object_id=object_id,
            format_version=1,
            metadata={"level": level},
        )

    def _matches(self, text: str) -> list[tuple[int, int, str]]:
        protected = [match.span() for match in TOKEN.finditer(text)]
        starts = [span[0] for span in protected]
        matches = []
        for rule in self.rules:
            budget = _MATCH_BUDGET.get()
            deadline = (
                budget[1]
                if budget and budget[0] is self
                else (time.monotonic() + self._regex_timeout)
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValidationError("privacy regex timeout")
            try:
                for match in rule.pattern.finditer(text, timeout=remaining):
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    start, end = match.span(rule.group)
                    for left, right in _outside_tokens(start, end, protected, starts):
                        matches.append((left, right, rule.level))
                        if len(matches) > self._limit * max(1, len(self.rules)):
                            raise ValidationError("privacy detection capacity exceeded")
            except TimeoutError:
                raise ValidationError("privacy regex timeout") from None
        # Resolve overlapping detections before deciding allow/redact. The
        # strongest level owns the union, so credentials cannot be downgraded.
        merged: list[tuple[int, int, str]] = []
        for start, end, level in sorted(matches):
            if merged and start < merged[-1][1]:
                left, right, previous = merged.pop()
                strongest = max((previous, level), key=lambda name: self.levels[name].rank)
                merged.append((left, max(right, end), strongest))
            else:
                merged.append((start, end, level))
        return merged

    def _mask(
        self, text: str, scope: Scope | None, detected: set[str], owner: str = "", label: str = ""
    ) -> str:
        result, cursor = [], 0
        if "[JW-PRIVATE:" in TOKEN.sub("", text):
            raise ValidationError("malformed privacy placeholder")
        offset = len(label) + 1 if label else 0
        labeled = label + "=" + text if label else text
        matches = [
            (max(0, start - offset), end - offset, level)
            for start, end, level in self._matches(labeled)
            if end > offset
        ]
        plain_matches = set(self._matches(text)) if label else set()
        if label and any(match not in plain_matches for match in matches):
            # A credential field protects its entire value, including whitespace;
            # a normal note containing a phone number still masks only that span.
            strongest = self._strongest({item[2] for item in matches})
            protected = [match.span() for match in TOKEN.finditer(text)]
            intervals = _outside_tokens(0, len(text), protected, [span[0] for span in protected])
            matches = [(start, end, strongest) for start, end in intervals]
        for start, end, level in matches:
            detected.add(level)
            if self.levels[level].write == "allow":
                continue
            result.append(text[cursor:start])
            if scope is None:
                result.append("[PRIVATE]")
            else:
                token = "[JW-PRIVATE:" + secrets.token_hex(24) + "]"
                cipher = self._crypto.encrypt(
                    text[start:end].encode(),
                    context=self._context(scope, token, "privacy-fragment", level),
                )
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
                    self._pending[(namespace, token)] = (now, cipher, level, owner, namespace)
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

            def protect(text: str, *, label: str = "") -> str:
                if "[JW-PRIVATE:" in text:
                    raise ValidationError("privacy placeholders are reserved for server use")
                return self._mask(text, payload.scope, detected, result.id, label)

            result.data = protect(payload.data.decode("utf-8")).encode("utf-8")
            result.user_metadata = _walk(result.user_metadata, protect)
            result.system_metadata = {
                key: value
                if key in KERNEL_SYSTEM_METADATA_KEYS | TRANSIENT_SYSTEM_METADATA_KEYS
                else _walk(value, protect, label=key)
                for key, value in result.system_metadata.items()
            }
            result.system_metadata[LEVEL_KEY] = self._strongest(detected)
            return result

    def _strongest(self, names: set[str]) -> str:
        return (
            max(names, key=lambda name: self.levels[name].rank)
            if names
            else min(self.levels, key=lambda name: self.levels[name].rank)
        )

    def inherit_classification(self, unit, sources) -> None:
        names = {
            source.system_metadata.get(LEVEL_KEY)
            for source in sources
            if source.scope == unit.scope
        }
        names.add(unit.system_metadata.get(LEVEL_KEY))
        unit.system_metadata[LEVEL_KEY] = self._strongest(names & self.levels.keys())

    def prepare(self, unit: MemoryUnit) -> None:
        with self._matching_scope():
            if unit.assets or any(
                segment.source not in {Modality.TEXT, Modality.CODE} for segment in unit.segments
            ):
                raise ValidationError("privacy currently supports TEXT/CODE without assets")
            before_content = unit.content
            detected: set[str] = set()
            previous = unit.system_metadata.get(LEVEL_KEY)
            if previous in self.levels:
                detected.add(previous)

            def protect(text: str, *, label: str = "") -> str:
                return self._mask(text, unit.scope, detected, unit.id, label)

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
            if before_content != unit.content:
                unit.vectors.clear()
            # Index-only builders must reject foreign references too, without
            # publishing a manifest or replacing a truth snapshot.
            with self._lock:
                references = {}
                for token in {match.group() for match in TOKEN.finditer(_json(_texts(unit)))}:
                    self._fragment(unit, token, references)

    def _fragment(self, unit: MemoryUnit, token: str, references: dict) -> tuple[bytes, str, str]:
        namespace = _scope(unit.scope)
        owners = {unit.id, unit.source_ref, unit.supersedes, *unit.provenance} - {""}
        staged = self._pending.get((namespace, token))
        if staged and time.monotonic() - staged[0] <= self._ttl and staged[3] in owners:
            return staged[1], staged[2], staged[4]
        sources = {prefix + "/" + uid for uid in owners for prefix in ("memory", "message")}
        if "tokens" not in references:
            references["tokens"] = set()
            if sources:
                placeholders = ",".join("?" for _ in sources)
                rows = self._db.execute(
                    "SELECT unit,revision,cipher,digest FROM manifests WHERE scope=? AND unit IN ("
                    + placeholders
                    + ") UNION ALL SELECT unit,revision,cipher,digest FROM recovery_manifests "
                    "WHERE scope=? AND unit IN (" + placeholders + ")",
                    (namespace, *sources, namespace, *sources),
                ).fetchall()
                for source, revision, manifest_cipher, digest in rows:
                    # Authenticate reference metadata once per source snapshot.
                    # Sensitive fragments are never decrypted by normal processing.
                    manifest = json.loads(
                        self._crypto.decrypt(
                            manifest_cipher,
                            context=self._context(
                                unit.scope, source + ":" + revision, "privacy-manifest"
                            ),
                        )
                    )
                    if manifest["digest"] != digest:
                        raise ValidationError("privacy reference manifest mismatch")
                    references["tokens"].update(manifest["tokens"])
        if token in references["tokens"]:
            row = self._db.execute(
                "SELECT cipher,level,origin FROM fragments WHERE scope=? AND token=?",
                (namespace, token),
            ).fetchone()
            if row is not None:
                return row
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
            for token, (cipher, level, origin) in fragments:
                pending[(_scope(target), token)] = (now, cipher, level, unit.id, origin)
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
        namespace, digest = _scope(unit.scope), _digest(unit)
        record_id = channel + "/" + unit.id
        tokens = sorted({match.group() for match in TOKEN.finditer(_json(_texts(unit)))})
        if "[JW-PRIVATE:" in TOKEN.sub("", _json(_texts(unit))):
            raise ValidationError("malformed privacy placeholder")
        revision = secrets.token_hex(16)
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT revision,digest,cipher FROM manifests WHERE scope=? AND unit=?",
                (namespace, record_id),
            ).fetchone()
            if existing and existing[1] == digest:
                authenticated = json.loads(
                    self._crypto.decrypt(
                        existing[2],
                        context=self._context(
                            unit.scope, record_id + ":" + existing[0], "privacy-manifest"
                        ),
                    )
                )
                if authenticated["digest"] != digest:
                    raise ValidationError("privacy reference manifest mismatch")
                if authenticated["tokens"] == tokens:
                    key = REVISION_KEY if channel == "memory" else MESSAGE_REVISION_KEY
                    unit.system_metadata[key] = existing[0]
                    level_rows = self._db.execute(
                        "SELECT f.level FROM fragments f JOIN bindings b "
                        "ON f.scope=b.scope AND f.token=b.token WHERE b.scope=? AND b.unit=?",
                        (namespace, record_id),
                    )
                    levels = {row[0] for row in level_rows}
                    unit.system_metadata[LEVEL_KEY] = self._strongest(
                        levels | {unit.system_metadata[LEVEL_KEY]}
                    )
                    return
            fragments = []
            references = {}
            for token in tokens:
                cipher, level, origin = self._fragment(unit, token, references)
                fragments.append((namespace, token, cipher, level, origin))
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
            manifest = self._crypto.encrypt(
                _json(
                    {
                        "tokens": tokens,
                        "digest": digest,
                        "segments": positions,
                    }
                ).encode(),
                context=self._context(unit.scope, record_id + ":" + revision, "privacy-manifest"),
            )
            self._db.executemany("INSERT OR IGNORE INTO fragments VALUES(?,?,?,?,?)", fragments)
            self._db.execute(
                "DELETE FROM manifests WHERE scope=? AND unit=?", (namespace, record_id)
            )
            self._db.execute(
                "INSERT INTO manifests VALUES(?,?,?,?,?)",
                (namespace, record_id, revision, digest, manifest),
            )
            self._db.executemany(
                "INSERT INTO bindings VALUES(?,?,?)",
                [(namespace, record_id, token) for token in tokens],
            )
            self._collect()
            key = REVISION_KEY if channel == "memory" else MESSAGE_REVISION_KEY
            unit.system_metadata[key] = revision
            unit.system_metadata[LEVEL_KEY] = self._strongest(
                {unit.system_metadata[LEVEL_KEY], *(fragment[3] for fragment in fragments)}
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
                        },
                    )
                )

    def refresh_revision(self, unit: MemoryUnit) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT revision,digest FROM manifests WHERE scope=? AND unit=?",
                (_scope(unit.scope), "memory/" + unit.id),
            ).fetchone()
            if row and row[1] == _digest(unit):
                unit.system_metadata[REVISION_KEY] = row[0]

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
        namespace, record_id = _scope(unit.scope), channel + "/" + unit.id
        with self._lock:
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO recovery_manifests "
                    "SELECT * FROM manifests WHERE scope=? AND unit=?",
                    (namespace, record_id),
                )
                self._db.execute(
                    "INSERT OR IGNORE INTO recovery_bindings "
                    "SELECT b.scope,b.unit,m.revision,b.token FROM bindings b "
                    "JOIN manifests m ON b.scope=m.scope AND b.unit=m.unit "
                    "WHERE b.scope=? AND b.unit=?",
                    (namespace, record_id),
                )
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
                with self._db:
                    self._db.execute(
                        "DELETE FROM recovery_manifests WHERE scope=? AND unit=?",
                        (namespace, record_id),
                    )
                    self._collect()

    def _reconcile(self, unit: MemoryUnit, *, channel: str) -> None:
        namespace, record_id = _scope(unit.scope), channel + "/" + unit.id
        key = REVISION_KEY if channel == "memory" else MESSAGE_REVISION_KEY
        revision, digest = unit.system_metadata.get(key), _digest(unit)
        with self._db:
            active = self._db.execute(
                "SELECT revision,digest FROM manifests WHERE scope=? AND unit=?",
                (namespace, record_id),
            ).fetchone()
            if active != (revision, digest):
                recovered = self._db.execute(
                    "SELECT * FROM recovery_manifests "
                    "WHERE scope=? AND unit=? AND revision=? AND digest=?",
                    (namespace, record_id, revision, digest),
                ).fetchone()
                if recovered is None:
                    return
                self._db.execute(
                    "DELETE FROM manifests WHERE scope=? AND unit=?", (namespace, record_id)
                )
                self._db.execute("INSERT INTO manifests VALUES(?,?,?,?,?)", recovered)
                self._db.execute(
                    "INSERT INTO bindings SELECT scope,unit,token FROM recovery_bindings "
                    "WHERE scope=? AND unit=? AND revision=?",
                    (namespace, record_id, revision),
                )
            self._db.execute(
                "DELETE FROM recovery_manifests WHERE scope=? AND unit=?", (namespace, record_id)
            )
            self._collect()

    def _collect(self) -> None:
        self._db.execute(
            "DELETE FROM fragments WHERE NOT EXISTS (SELECT 1 FROM bindings b "
            "WHERE b.scope=fragments.scope AND b.token=fragments.token) "
            "AND NOT EXISTS (SELECT 1 FROM recovery_bindings b "
            "WHERE b.scope=fragments.scope AND b.token=fragments.token)"
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
        with self._db:
            tokens = set()
            for uid in unit_ids:
                token_rows = self._db.execute(
                    "SELECT token FROM bindings WHERE scope=? AND unit=? "
                    "UNION SELECT token FROM recovery_bindings WHERE scope=? AND unit=?",
                    (_scope(scope), uid) * 2,
                )
                tokens.update(row[0] for row in token_rows)
            self._db.executemany(
                "DELETE FROM manifests WHERE scope=? AND unit=?",
                [(_scope(scope), uid) for uid in unit_ids],
            )
            self._db.executemany(
                "DELETE FROM recovery_manifests WHERE scope=? AND unit=?",
                [(_scope(scope), uid) for uid in unit_ids],
            )
            for token in tokens:
                self._pending.pop((_scope(scope), token), None)
            self._collect()

    def discard_scope(self, scope: Scope) -> None:
        selected = {key: value for key, value in asdict(scope).items() if value}
        with self._lock, self._db:
            scope_rows = self._db.execute(
                "SELECT scope FROM manifests UNION SELECT scope FROM recovery_manifests"
            )
            namespaces = {row[0] for row in scope_rows}
            namespaces.update(key[0] for key in self._pending)
            for namespace in namespaces:
                actual = json.loads(namespace)
                if all(actual[key] == value for key, value in selected.items()):
                    self._db.execute("DELETE FROM manifests WHERE scope=?", (namespace,))
                    self._db.execute("DELETE FROM recovery_manifests WHERE scope=?", (namespace,))
                    self._pending = {
                        key: value for key, value in self._pending.items() if key[0] != namespace
                    }
            self._collect()

    def render(self, unit: MemoryUnit) -> str:
        # Caller must be the human disclosure service; this method is never
        # exported through MemoryAPI, SDK, MCP or the retrieval runtime.
        with self._lock:
            row = self._db.execute(
                "SELECT revision,digest,cipher FROM manifests "
                "WHERE scope=? AND unit=? AND revision=? UNION ALL "
                "SELECT revision,digest,cipher FROM recovery_manifests "
                "WHERE scope=? AND unit=? AND revision=?",
                (
                    _scope(unit.scope),
                    "memory/" + unit.id,
                    unit.system_metadata.get(REVISION_KEY),
                )
                * 2,
            ).fetchone()
            if (
                not row
                or row[0] != unit.system_metadata.get(REVISION_KEY)
                or row[1] != _digest(unit)
            ):
                raise ValidationError("privacy snapshot is no longer available")
            manifest = json.loads(
                self._crypto.decrypt(
                    row[2],
                    context=self._context(
                        unit.scope, "memory/" + unit.id + ":" + row[0], "privacy-manifest"
                    ),
                )
            )
            if manifest["digest"] != _digest(unit):
                raise ValidationError("privacy snapshot mismatch")
            values = {}
            for token in manifest["tokens"]:
                fragment = self._db.execute(
                    "SELECT cipher,level,origin FROM fragments WHERE scope=? AND token=?",
                    (_scope(unit.scope), token),
                ).fetchone()
                if fragment is None or fragment[1] not in self.levels:
                    raise ValidationError("privacy fragment is no longer available")
                if self.levels[fragment[1]].reveal:
                    values[token] = self._crypto.decrypt(
                        fragment[0],
                        context=self._context(
                            Scope(**json.loads(fragment[2])), token, "privacy-fragment", fragment[1]
                        ),
                    ).decode("utf-8")
            return TOKEN.sub(lambda match: values.get(match.group(), match.group()), unit.content)


@PrivacyProducer.register("regex")
def _build(config):
    if config.get("llm_enabled", False):
        raise ValidationError("privacy LLM classification is not implemented")
    if not config.get("cryptography"):
        raise ValidationError("privacy requires explicit cryptography configuration")
    path = config.get("vault_path")
    if not path:
        raise ValidationError("privacy requires a dedicated vault_path")
    return RegexPrivacy(
        CryptographyProducer.dep(config),
        str(path),
        levels=config.get("levels"),
        rules=config.get("rules"),
        staging_ttl=int(config.get("staging_ttl", 300)),
        staging_limit=int(config.get("staging_limit", 10000)),
        regex_timeout=config.get("regex_timeout", 2.0),
    )
