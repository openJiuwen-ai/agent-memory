"""Protect direct truth/raw-message writes as well as IndexBuilder writes."""

from contextvars import ContextVar

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.security.privacy.base import PrivacyProtection, privacy_operation
from jiuwen_memory.common.type_def.memory import MEMORY_KEY_PREFIX
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.common.type_def.raw import MESSAGES_KEY_PREFIX, messages_key
from jiuwen_memory.storage.kv import KVStore

_ACTIVE_KV: ContextVar[frozenset[tuple]] = ContextVar("privacy_kv_operation", default=frozenset())


class PrivacyKVStore(KVStore):
    def __init__(self, delegate: KVStore, privacy: PrivacyProtection) -> None:
        self._delegate, self._privacy = delegate, privacy

    @property
    def security(self):
        return self._delegate.security

    def health(self):
        self._delegate.health()
        self._privacy.health()

    def store_type(self):
        return self._delegate.store_type()

    @privacy_operation
    def _write(self, method, scope, key, value, ttl):
        if not key.startswith((MEMORY_KEY_PREFIX, MESSAGES_KEY_PREFIX)):
            return method(scope, key, value, ttl)
        operation = (
            id(self._privacy),
            scope.org,
            scope.space,
            scope.user,
            scope.agent,
            scope.session,
            key,
        )
        # Inner raw ports of encrypted/composed stores receive already-protected
        # ciphertext. Only a KV wrapper's active operation can skip this boundary.
        if operation in _ACTIVE_KV.get():
            return method(scope, key, value, ttl)
        channel = "message" if key.startswith(MESSAGES_KEY_PREFIX) else "memory"
        unit = loads(value)
        expected_key = (
            messages_key(unit.id)
            if unit and channel == "message"
            else MEMORY_KEY_PREFIX + (unit.id if unit else "")
        )
        if unit is None:
            raise ValidationError("invalid privacy message record")
        if unit.scope != scope or expected_key != key or ttl:
            raise ValidationError("invalid privacy message record")
        marker = _ACTIVE_KV.set(_ACTIVE_KV.get() | {operation})

        def read():
            try:
                return loads(self._delegate.get(scope, key))
            except NotFoundError:
                return None

        try:
            self._privacy.write(
                unit,
                lambda protected: method(scope, key, dumps(protected), ttl),
                read,
                channel=channel,
            )
        finally:
            _ACTIVE_KV.reset(marker)

    def insert(self, scope, key, value, ttl=0.0):
        self._write(self._delegate.insert, scope, key, value, ttl)

    def update(self, scope, key, value, ttl=0.0):
        self._write(self._delegate.update, scope, key, value, ttl)

    def delete(self, scope, key):
        for prefix, channel in ((MEMORY_KEY_PREFIX, "memory"), (MESSAGES_KEY_PREFIX, "message")):
            if key.startswith(prefix):
                self._privacy.discard(
                    scope,
                    [key.removeprefix(prefix)],
                    channel=channel,
                    commit=lambda: self._delegate.delete(scope, key),
                )
                return
        self._delegate.delete(scope, key)

    def get(self, scope, key):
        return self._delegate.get(scope, key)

    def mget(self, scope, keys):
        return self._delegate.mget(scope, keys)

    def exists(self, scope, key):
        return self._delegate.exists(scope, key)

    def scan(self, scope, prefix=""):
        return self._delegate.scan(scope, prefix)

    def list(self, scope, **kwargs):
        return self._delegate.list(scope, **kwargs)

    def scopes(self):
        return self._delegate.scopes()
