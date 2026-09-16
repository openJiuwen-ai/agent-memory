# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Redis-native Source sets. All operations bind to one concrete client and Scope.

The internal namespace has no ':' and cannot be a normal five-part KV namespace.
Only explicit maintenance scans it. Normal lookup uses SMEMBERS and bounded MGET.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, Scope

from .schema_source_index import MemorySourceIndex, property_sources

INTERNAL_PREFIX = "__jiuwen_schema_sources_v1__/"
_BATCH = 256
_READY = b"ready:1:"

# KEYS: body, readiness, property sources, all affected source sets.
# ARGV: operation, body, ttl_ms, old sources, new sources, old readiness, set membership flags.
# Compare the old sources before touching any key. Lua errors do not roll back:
# readiness is invalidated before mutation and only restored on complete success.
_WRITE = """
local function kind(key) return redis.call('TYPE', key).ok end
local function corrupt()
    redis.call('SET', KEYS[2], 'invalid')
    return redis.error_reply('invalid schema source index key type')
end
for i = 1, 3 do
    local t = kind(KEYS[i])
    if t ~= 'none' and t ~= 'string' then return corrupt() end
end
local state = redis.call('GET', KEYS[2]) or ''
if state ~= ARGV[6] then return 0 end
local previous = redis.call('GET', KEYS[3]) or ''
if previous ~= ARGV[4] then return 0 end
local exists = redis.call('EXISTS', KEYS[1])
if ARGV[1] == 'insert' and exists == 1 then return -1 end
if ARGV[1] == 'update' and exists == 0 then return -2 end
for i = 4, #KEYS do
    local t = kind(KEYS[i])
    if t ~= 'none' and t ~= 'set' then return corrupt() end
end
redis.call('SET', KEYS[2], 'invalid')
if ARGV[1] == 'delete' then
    redis.call('DEL', KEYS[1])
elseif tonumber(ARGV[3]) > 0 then
    redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
else
    redis.call('SET', KEYS[1], ARGV[2])
end
for i = 4, #KEYS do
    if ARGV[i + 3] == '1' then
        redis.call('SADD', KEYS[i], ARGV[#KEYS + 4])
    else
        redis.call('SREM', KEYS[i], ARGV[#KEYS + 4])
    end
end
if ARGV[5] == '' then
    redis.call('DEL', KEYS[3])
else
    redis.call('SET', KEYS[3], ARGV[5])
end
if string.sub(state, 1, 8) == 'ready:1:' then
    redis.call('SET', KEYS[2], state)
end
return 1
"""

_PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""


def _encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode()


def internal_scope(key: str) -> Scope | None:
    """Return the real Scope of internal data, including TTL-only orphan relations."""
    if not key.startswith(INTERNAL_PREFIX):
        return None
    token = key[len(INTERNAL_PREFIX):].split("/", 1)[0]
    values = json.loads(base64.urlsafe_b64decode(token))
    return Scope(org=values[0], space=values[1], user=values[2], agent=values[3], session=values[4])


class RedisSchemaSourceIndex:
    def __init__(self, client: Any, scope: Scope, body_prefix: str) -> None:
        self.client = client
        self.body_prefix = body_prefix
        dims = [scope.org, scope.space, scope.user, scope.agent, scope.session]
        self.prefix = INTERNAL_PREFIX + _encode(json.dumps(dims, ensure_ascii=False)) + "/"
        self.state_key = self.prefix + "state"

    def source_key(self, source_id: str) -> str:
        return self.prefix + "source/" + _encode(source_id)

    def property_key(self, key: str) -> str:
        return self.prefix + "property/" + _encode(key)

    def write(self, operation: str, key: str, raw: bytes = b"", ttl_ms: int | None = None) -> None:
        if ttl_ms == 0:
            raise BackendError("Redis expiry must be at least one millisecond")
        new = property_sources(key, raw) if operation != "delete" else set()
        new_raw = json.dumps(sorted(new)).encode() if new else b""
        reverse_key = self.property_key(key)
        for _ in range(3):
            state, previous = self.client.mget([self.state_key, reverse_key])
            try:
                decoded = json.loads(previous) if previous else []
                if not isinstance(decoded, list):
                    raise ValueError("expected a source list")
                old = set(decoded)
                if not all(isinstance(source, str) for source in old):
                    raise ValueError("non-string source ID")
            except (TypeError, ValueError) as exc:
                self.client.set(self.state_key, b"invalid")
                raise BackendError("Invalid stored Schema property sources") from exc
            affected = sorted(old | new)
            keys = [self.body_prefix + key, self.state_key, reverse_key]
            keys.extend(self.source_key(source) for source in affected)
            args = [operation, raw, ttl_ms or 0, previous or b"", new_raw, state or b""]
            args.extend(int(source in new) for source in affected)
            args.append(key)
            result = self.client.eval(_WRITE, len(keys), *keys, *args)
            if result == 1:
                return
            if result == -1:
                raise ConflictError("key", key)
            if result == -2:
                raise NotFoundError("key", key)
        raise ConflictError("schema source index", key)

    def get(self, source_id: str) -> list[tuple[str, bytes]] | None:
        state = self.client.get(self.state_key)
        if state is None or not state.startswith(_READY):
            return None
        keys = list(self.client.smembers(self.source_key(source_id)))
        entries = []
        for offset in range(0, len(keys), _BATCH):
            logical = [key.decode() for key in keys[offset:offset + _BATCH]]
            physical = [self.body_prefix + key for key in logical]
            for key, raw in zip(logical, self.client.mget(physical)):
                if raw is not None:
                    entries.append((key, raw))
        if self.client.get(self.state_key) != state:
            return None
        return entries

    def clear(self) -> None:
        # Absence also means unready. Invalidate before deleting any relationship.
        self.client.set(self.state_key, b"invalid")
        self._clear_relations()
        self.client.delete(self.state_key)

    def _clear_relations(self) -> None:
        keys = []
        for key in self.client.scan_iter(match=self.prefix + "*", count=_BATCH):
            if key.decode() != self.state_key:
                keys.append(key)
        for offset in range(0, len(keys), _BATCH):
            self.client.delete(*keys[offset:offset + _BATCH])

    def rebuild(self) -> None:
        """Caller must pause all Scope writes and serialize maintenance operations."""
        policy = self.client.config_get("maxmemory-policy").get("maxmemory-policy")
        if policy != "noeviction":
            raise ValidationError("Schema source index requires Redis maxmemory-policy=noeviction")
        token = b"building:" + uuid.uuid4().hex.encode()
        self.client.set(self.state_key, token)
        self._clear_relations()
        expected = MemorySourceIndex()
        # Escape Redis glob metacharacters in the existing body's Scope prefix.
        match = self.body_prefix
        for char in ("\\", "*", "?", "[", "]"):
            match = match.replace(char, "\\" + char)
        batch = []
        for physical in self.client.scan_iter(match=match + MEMORY_KEY_PREFIX + "*", count=_BATCH):
            batch.append(physical)
            if len(batch) == _BATCH:
                self._project_batch(expected, batch)
                batch.clear()
        self._project_batch(expected, batch)
        for source, keys in expected.by_source.items():
            members = sorted(keys)
            for offset in range(0, len(members), _BATCH):
                self.client.sadd(self.source_key(source), *members[offset:offset + _BATCH])
        for key, sources in expected.by_property.items():
            self.client.set(self.property_key(key), json.dumps(sorted(sources)).encode())
        for source, keys in expected.by_source.items():
            actual = self.client.smembers(self.source_key(source))
            if actual != {key.encode() for key in keys}:
                raise BackendError("Schema source index verification failed")
        for key, sources in expected.by_property.items():
            actual = self.client.get(self.property_key(key))
            if actual != json.dumps(sorted(sources)).encode():
                raise BackendError("Schema property source verification failed")
        ready = _READY + uuid.uuid4().hex.encode()
        if not self.client.eval(_PUBLISH, 1, self.state_key, token, ready):
            raise ConflictError("schema source index", "concurrent write during rebuild")

    def _project_batch(self, index: MemorySourceIndex, keys: list[bytes]) -> None:
        if not keys:
            return
        for physical, raw in zip(keys, self.client.mget(keys)):
            if raw is not None:
                key = physical.decode()[len(self.body_prefix):]
                index.replace(key, property_sources(key, raw))
