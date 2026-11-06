"""Explicit scoped maintenance and unchanged plaintext read errors through public API."""

import sqlite3
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from deploy.migration.runner import migrate_plaintext_memories
from jiuwen_memory.api import (
    Context,
    Credentials,
    MemoryUnit,
    Scope,
    Segment,
    assemble_runtime,
)
from jiuwen_memory.common.errors import BackendError, NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.integration
ALICE = Scope(org="acme", user="alice")
BOB = Scope(org="acme", user="bob")
OTHER = Scope(org="other", user="carol")


def configuration(tmp_path):
    identities = {
        "alice-token": {"actor": asdict(ALICE)},
        "admin-token": {"actor": {"org": "acme", "user": "admin"}, "role": "admin"},
        "root-token": {"actor": {"org": "system", "user": "root"}, "role": "root"},
    }
    return {
        "globals": {
            "storage_encryption_enabled": True,
            "vector_enabled": False,
            "graph_enabled": False,
            "layers_index_enabled": False,
        },
        "kv_store": {
            "default": {"target": "sqlite", "params": {"db_path": str(tmp_path / "memory.db")}}
        },
        "key_provider": {"default": {"target": "local", "params": {"key_hex": "11" * 32}}},
        "cryptography": {"default": {"target": "local", "params": {"key_provider": "default"}}},
        "authenticator": {"default": {"target": "dev", "params": {"identities": identities}}},
        "security": {"default": {"target": "standard", "params": {"authenticator": "default"}}},
    }


def seed(tmp_path, scope=ALICE, unit_id="id-1", raw_value=None, ttl=0):
    unit = MemoryUnit(id=unit_id, scope=scope, segments=[Segment(content="secret memory")])
    raw = dumps(unit) if raw_value is None else raw_value
    store = SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        store.insert(scope, memory_key(unit_id), raw, ttl=ttl)
    finally:
        store.close()
    return raw


def value(tmp_path, scope=ALICE, unit_id="id-1"):
    store = SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        return store.get(scope, memory_key(unit_id))
    finally:
        store.close()


@pytest.mark.parametrize("method", ["get", "list", "search", "inspect", "trace"])
def test_runtime_raises_existing_backend_error_without_modifying_source(tmp_path, method):
    before = seed(tmp_path)
    created = []
    with Factory.observe_builds(created.append):
        runtime = assemble_runtime(config=configuration(tmp_path))
    try:
        security = runtime.authenticate(Credentials(api_key="alice-token"))
        if method == "search":
            units = runtime.api.add("secret memory", ALICE, security=security)
            store = SQLiteKVStore(str(tmp_path / "memory.db"))
            try:
                store.update(ALICE, memory_key(units[0].id), dumps(units[0]))
            finally:
                store.close()
        arguments = {
            "get": ("id-1", ALICE),
            "list": (ALICE,),
            "search": ("secret", Context(scope=ALICE)),
            "inspect": (["id-1"], ALICE),
            "trace": ("id-1", ALICE),
        }
        method_arguments = arguments.get(method)
        assert method_arguments is not None, f"missing arguments for {method}"
        with pytest.raises(BackendError, match="explicit migration or clearing"):
            getattr(runtime.api, method)(*method_arguments, security=security)
    finally:
        runtime.close()
    assert value(tmp_path) == before


@pytest.mark.parametrize(
    "token,expected",
    [
        ("alice-token", 1),
        ("admin-token", 2),
        ("root-token", 3),
    ],
)
def test_scan_migrates_only_authenticated_role_scope_and_repeats_safely(tmp_path, token, expected):
    for scope in (ALICE, BOB, OTHER):
        seed(tmp_path, scope)
    data = configuration(tmp_path)
    result = migrate_plaintext_memories(
        config=data, credentials=Credentials(api_key=token), operation="migrate"
    )
    assert result.status == "complete", result
    assert len(result.items) == expected
    assert all(item.status == "migrated" for item in result.items)
    assert (
        sum(value(tmp_path, scope).startswith(b"ENC1") for scope in (ALICE, BOB, OTHER)) == expected
    )
    repeated = migrate_plaintext_memories(
        config=data, credentials=Credentials(api_key=token), operation="migrate"
    )
    assert repeated.status == "complete" and repeated.items == (), repeated


@pytest.mark.parametrize("operation", ["migrate", "clear"])
@pytest.mark.parametrize("from_file", [False, True])
@pytest.mark.parametrize("actor_space", ["", "team"])
def test_user_maintenance_cannot_cross_space(tmp_path, operation, from_file, actor_space):
    actor = replace(ALICE, space=actor_space)
    scopes = [replace(ALICE, space=space) for space in ("", "team", "other")]
    original = [(scope, seed(tmp_path, scope)) for scope in scopes]
    data = configuration(tmp_path)
    data["authenticator"]["default"]["params"]["identities"]["alice-token"]["actor"] = asdict(
        actor
    )
    result = migrate_plaintext_memories(
        [raw for _, raw in original] if from_file else None,
        config=data,
        credentials=Credentials(api_key="alice-token"),
        operation=operation,
    )
    assert result.status == ("failed" if from_file else "complete"), result
    assert sum(item.committed for item in result.items) == 1, result
    if from_file:
        denied = [item for item in result.items if item.status == "failed"]
        assert len(denied) == 2 and all(item.error == "permission_denied" for item in denied)
    else:
        assert len(result.items) == 1, "scanning must not expose other spaces"
    for scope, raw in original:
        if scope != actor:
            assert value(tmp_path, scope) == raw, "unauthorized space must remain unchanged"
        elif operation == "migrate":
            assert value(tmp_path, scope).startswith(b"ENC1")
        else:
            with pytest.raises(NotFoundError):
                value(tmp_path, scope)


def test_inspection_is_read_only_and_clear_never_removes_ciphertext(tmp_path):
    before = seed(tmp_path)
    seed(tmp_path, BOB)
    data = configuration(tmp_path)
    credentials = Credentials(api_key="alice-token")
    inspected = migrate_plaintext_memories(config=data, credentials=credentials)
    assert inspected.status == "complete" and inspected.items[0].status == "plaintext", inspected
    assert value(tmp_path) == before
    cleared = migrate_plaintext_memories(config=data, credentials=credentials, operation="clear")
    assert cleared.status == "complete" and cleared.items[0].status == "cleared", cleared
    assert value(tmp_path, BOB)
    imported = migrate_plaintext_memories(
        [before], config=data, credentials=credentials, operation="migrate"
    )
    assert imported.status == "complete", imported
    ciphertext = value(tmp_path)
    repeated_clear = migrate_plaintext_memories(
        [before], config=data, credentials=credentials, operation="clear"
    )
    assert repeated_clear.items[0].status == "skipped"
    assert value(tmp_path) == ciphertext


def test_scanning_validates_record_scope_and_key_and_returns_errors(tmp_path):
    seed(tmp_path, raw_value=b"broken")
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="migrate",
    )
    assert result.status == "failed" and result.items[0].error == "invalid_input", result
    assert value(tmp_path) == b"broken"


def test_migration_preserves_absolute_expiry(tmp_path):
    seed(tmp_path, ttl=120)
    with closing(sqlite3.connect(tmp_path / "memory.db")) as connection:
        before = connection.execute("SELECT expires_at FROM kv").fetchone()[0]
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="migrate",
    )
    assert result.status == "complete", result
    with closing(sqlite3.connect(tmp_path / "memory.db")) as connection:
        after = connection.execute("SELECT expires_at FROM kv").fetchone()[0]
    assert before == after


def test_file_cannot_claim_another_users_scope_or_clear_ciphertext(tmp_path):
    raw = seed(tmp_path, BOB)
    result = migrate_plaintext_memories(
        [raw],
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="clear",
    )
    assert result.status == "failed" and result.items[0].error == "permission_denied", result
    assert value(tmp_path, BOB) == raw


def test_invalid_credentials_do_not_enumerate_or_mutate(tmp_path, monkeypatch):
    raw = seed(tmp_path)

    def unexpected_scan(self):
        pytest.fail("unauthenticated tool reached backend enumeration")

    monkeypatch.setattr(SQLiteKVStore, "scopes", unexpected_scan)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="invalid"),
        operation="migrate",
    )
    assert result.error == "authentication_failed" and result.items == (), result
    assert value(tmp_path) == raw


def test_scanner_does_not_load_another_users_values(tmp_path, monkeypatch):
    seed(tmp_path, BOB, raw_value=b"broken foreign plaintext")
    original = SQLiteKVStore.scan

    def checked_scan(self, scope, prefix=""):
        assert scope != BOB, "foreign Scope was loaded before authorization"
        return original(self, scope, prefix)

    monkeypatch.setattr(SQLiteKVStore, "scan", checked_scan)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path), credentials=Credentials(api_key="alice-token")
    )
    assert result.status == "complete" and not result.items, result


def test_scanned_record_disappearing_does_not_get_recreated(tmp_path, monkeypatch):
    from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore

    seed(tmp_path)
    original = EncryptedKVStore.migrate_plaintext

    def expire(self, scope, key, expected, **kwargs):
        store = SQLiteKVStore(str(tmp_path / "memory.db"))
        try:
            store.delete(scope, key)
        finally:
            store.close()
        return original(self, scope, key, expected, **kwargs)

    monkeypatch.setattr(EncryptedKVStore, "migrate_plaintext", expire)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="migrate",
    )
    assert result.status == "complete" and result.items[0].status == "skipped", result
    with closing(sqlite3.connect(tmp_path / "memory.db")) as connection:
        assert (
            connection.execute("SELECT count(*) FROM kv WHERE key='/memory/id-1'").fetchone()[0]
            == 0
        )


def test_competing_write_is_not_overwritten_by_clear(tmp_path, monkeypatch):
    seed(tmp_path)
    original = SQLiteKVStore.compare_exchange

    def compete(self, scope, key, expected, replacement):
        store = SQLiteKVStore(str(tmp_path / "memory.db"))
        try:
            store.update(scope, key, b"newer value")
        finally:
            store.close()
        return original(self, scope, key, expected, replacement)

    monkeypatch.setattr(SQLiteKVStore, "compare_exchange", compete)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="clear",
    )
    assert result.items[0].error == "target_conflict", result
    assert value(tmp_path) == b"newer value"


def test_unknown_fs_capability_refuses_before_any_kv_mutation(tmp_path):
    raw = seed(tmp_path)
    config = configuration(tmp_path)
    config["fs_store"] = {
        "default": {"target": "local", "params": {"root": str(tmp_path / "assets")}}
    }
    result = migrate_plaintext_memories(
        config=config, credentials=Credentials(api_key="alice-token"), operation="migrate"
    )
    assert result.status == "failed" and result.items[0].source == "fs_store.default", result
    assert value(tmp_path) == raw


def test_physical_aliases_are_deduplicated(tmp_path):
    seed(tmp_path)
    config = configuration(tmp_path)
    config["kv_store"]["alias"] = config["kv_store"]["default"]
    result = migrate_plaintext_memories(
        config=config, credentials=Credentials(api_key="alice-token"), operation="migrate"
    )
    assert result.status == "complete" and len(result.items) == 1, result


def test_partial_batch_keeps_success_and_reports_bad_record(tmp_path):
    raw = seed(tmp_path)
    result = migrate_plaintext_memories(
        [raw, b"bad memory"],
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="migrate",
    )
    assert result.status == "failed", result
    assert result.items[0].status == "migrated" and result.items[0].committed
    assert result.items[1].error == "invalid_input" and not result.items[1].committed
    assert value(tmp_path).startswith(b"ENC1")


def test_persisted_ciphertext_readback_failure_is_reported_as_committed(tmp_path, monkeypatch):
    seed(tmp_path)
    original = SQLiteKVStore.compare_exchange

    def corrupt(self, scope, key, expected, replacement):
        committed = original(self, scope, key, expected, replacement)
        if committed and replacement is not None:
            self.update(scope, key, b"ENC1damaged")
        return committed

    monkeypatch.setattr(SQLiteKVStore, "compare_exchange", corrupt)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="migrate",
    )
    assert result.status == "failed" and result.items[0].error == "verification_failed", result
    assert result.items[0].committed and value(tmp_path) == b"ENC1damaged"


def test_cli_scans_configured_source_without_export_file(tmp_path, monkeypatch, capsys):
    import json

    import yaml

    from deploy.migration.encrypt import main

    before = seed(tmp_path)
    config_file = tmp_path / "kernel.yaml"
    config_file.write_text(yaml.safe_dump(configuration(tmp_path)), encoding="utf-8")
    monkeypatch.setenv("AGENT_MEMORY_API_KEY", "alice-token")
    assert main(["--config", str(config_file), "--operation", "inspect"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["items"][0]["status"] == "plaintext"
    assert value(tmp_path) == before
    assert main(["--config", str(config_file), "--operation", "migrate"]) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["items"][0]["status"] == "migrated"
    assert value(tmp_path).startswith(b"ENC1")


def test_cli_clear_returns_structured_failure_for_bad_input(tmp_path, monkeypatch, capsys):
    import json

    import yaml

    from deploy.migration.encrypt import main

    before = seed(tmp_path)
    config_file = tmp_path / "kernel.yaml"
    config_file.write_text(yaml.safe_dump(configuration(tmp_path)), encoding="utf-8")
    input_file = tmp_path / "bad.jsonl"
    input_file.write_bytes(b"invalid memory\n")
    monkeypatch.setenv("AGENT_MEMORY_API_KEY", "alice-token")
    assert (
        main(["--config", str(config_file), "--operation", "clear", "--input", str(input_file)])
        == 2
    )
    body = json.loads(capsys.readouterr().out)
    assert body["items"][0]["error"] == "invalid_input"
    assert value(tmp_path) == before


def test_normal_plaintext_api_writes_can_be_migrated_and_read_encrypted(tmp_path):
    config = configuration(tmp_path)
    config["globals"]["storage_encryption_enabled"] = False
    runtime = assemble_runtime(config=config)
    try:
        security = runtime.authenticate(Credentials(api_key="alice-token"))
        units = runtime.api.add("a user's ordinary stored memory", ALICE, security=security)
        original = units[0]
    finally:
        runtime.close()
    assert not value(tmp_path, unit_id=original.id).startswith(b"ENC1")
    config["globals"]["storage_encryption_enabled"] = True
    result = migrate_plaintext_memories(
        config=config, credentials=Credentials(api_key="alice-token"), operation="migrate"
    )
    assert result.status == "complete" and result.items[0].status == "migrated", result
    runtime = assemble_runtime(config=config)
    try:
        security = runtime.authenticate(Credentials(api_key="alice-token"))
        restored = runtime.api.get(original.id, ALICE, security=security)
        assert restored.id == original.id and restored.scope == original.scope
        assert restored.content == original.content
        assert restored.system_metadata == original.system_metadata
        assert restored.user_metadata == original.user_metadata
    finally:
        runtime.close()


@pytest.mark.parametrize("fail_on,committed", [(1, False), (2, True)])
def test_audit_failure_does_not_hide_commit_state(tmp_path, monkeypatch, fail_on, committed):
    from jiuwen_memory.common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger

    raw = seed(tmp_path)
    original = SqliteAuditLogger.record
    calls = 0

    def fail_audit(self, event):
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise OSError("audit unavailable")
        return original(self, event)

    monkeypatch.setattr(SqliteAuditLogger, "record", fail_audit)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="migrate",
    )
    assert result.status == "failed" and result.items[0].committed is committed, result
    if committed:
        assert value(tmp_path).startswith(b"ENC1")
    else:
        assert value(tmp_path) == raw


@pytest.mark.parametrize("operation", ["inspect", "migrate", "clear"])
@pytest.mark.parametrize("use_input", [False, True])
def test_public_maintenance_rejects_invalid_dual_principal_identity(
    tmp_path, monkeypatch, operation, use_input
):
    from jiuwen_memory.common.security.authentication.authentication_impl.dev_authenticator import (
        DevAuthenticator,
    )

    actor = replace(ALICE, space="space", agent="agent-a")
    target = replace(actor, agent="agent-b")
    raw = seed(tmp_path, target)
    data = configuration(tmp_path)
    authenticate = DevAuthenticator.authenticate

    def bound_identity(self, credentials):
        # Even a plugin cannot introduce an invalid dual-principal actor at this boundary.
        return replace(authenticate(self, credentials), actor=actor)

    monkeypatch.setattr(DevAuthenticator, "authenticate", bound_identity)
    result = migrate_plaintext_memories(
        [raw] if use_input else None,
        config=data,
        credentials=Credentials(api_key="alice-token"),
        operation=operation,
    )
    assert result.status == "failed" and result.error == "authentication_failed", result
    assert result.items == ()
    assert value(tmp_path, target) == raw


@pytest.mark.parametrize("operation", ["inspect", "migrate", "clear"])
@pytest.mark.parametrize("use_input", [False, True])
def test_lossy_scope_is_rejected_for_scan_and_import(tmp_path, monkeypatch, operation, use_input):
    from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore

    # Exercise the real Redis lossless-scope contract with a local durable backend.
    target = replace(ALICE, user="alice:agent")
    raw = seed(tmp_path, target)
    monkeypatch.setattr(
        SQLiteKVStore, "migration_scope_supported", RedisKVStore.migration_scope_supported
    )
    result = migrate_plaintext_memories(
        [raw] if use_input else None,
        config=configuration(tmp_path),
        credentials=Credentials(api_key="root-token"),
        operation=operation,
    )
    assert result.status == "failed" and result.items[0].error == "unsupported", result
    assert not result.items[0].committed
    assert value(tmp_path, target) == raw


@pytest.mark.parametrize("operation", ["inspect", "migrate", "clear"])
def test_historical_jsonl_returns_format_upgrade_required(tmp_path, operation):
    import json

    payload = json.loads(seed(tmp_path))
    payload["_v"] = 3
    result = migrate_plaintext_memories(
        [json.dumps(payload).encode()],
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation=operation,
    )
    assert result.status == "failed", result
    assert result.items[0].error == "format_upgrade_required" and not result.items[0].committed
    assert not value(tmp_path).startswith(b"ENC1")


def test_maintenance_does_not_construct_business_runtime_and_closes_stores(tmp_path, monkeypatch):
    from jiuwen_memory.control.engine import EngineProducer
    from jiuwen_memory.control.ingest_job import IngestJobProducer
    from jiuwen_memory.control.scheduler import SchedulerProducer
    from jiuwen_memory.storage.watchdog import WatchdogProducer

    def unexpected_build(*args, **kwargs):
        pytest.fail("maintenance constructed a business runtime component")

    for producer in (EngineProducer, IngestJobProducer, SchedulerProducer, WatchdogProducer):
        monkeypatch.setattr(producer, "build", unexpected_build)
    seed(tmp_path)
    closed = []
    close = SQLiteKVStore.close

    def observed_close(self):
        closed.append(id(self))
        return close(self)

    monkeypatch.setattr(SQLiteKVStore, "close", observed_close)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path), credentials=Credentials(api_key="alice-token")
    )
    assert result.status == "complete", result
    assert len(closed) == 1, "the owned KV store must be closed once"


@pytest.mark.parametrize("operation", ["migrate", "clear"])
@pytest.mark.parametrize("role,expected", [("user", 1), ("admin", 2), ("root", 3)])
def test_real_api_key_maintenance_enforces_role_scope(tmp_path, operation, role, expected):
    from jiuwen_memory.common.bootstrap import register_plugins
    from jiuwen_memory.common.security.authentication.key_store import KeyStoreProducer
    from jiuwen_memory.common.security.types import Role

    register_plugins()
    keys = KeyStoreProducer.build("memory", {}, AssemblyContext())
    token = "maintenance-root-test-key"
    if role != "root":
        token = keys.issue(ALICE, Role(role))
    data = configuration(tmp_path)
    data["authenticator"] = {
        "default": {
            "target": "api_key",
            "params": {"key_store": "default", "root_api_key": "maintenance-root-test-key"},
        }
    }
    data["key_store"] = {"default": {"target": "maintenance_test_keys"}}
    for scope in (ALICE, BOB, OTHER):
        seed(tmp_path, scope)
    with KeyStoreProducer.registration_scope():
        @KeyStoreProducer.register("maintenance_test_keys")
        def shared_keys(config):
            return keys

        result = migrate_plaintext_memories(
            config=data, credentials=Credentials(api_key=token), operation=operation
        )
    assert result.status == "complete" and len(result.items) == expected, result
    assert all(item.committed for item in result.items)
    store = SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        scopes = (ALICE, BOB, OTHER)
        for position, scope in enumerate(scopes):
            if operation == "clear":
                assert store.exists(scope, memory_key("id-1")) is (position >= expected)
            else:
                encrypted = store.get(scope, memory_key("id-1")).startswith(b"ENC1")
                assert encrypted is (position < expected)
    finally:
        store.close()


def test_revoked_api_key_cannot_commit_after_authentication(tmp_path):
    from jiuwen_memory.common.bootstrap import register_plugins
    from jiuwen_memory.common.security.authentication.key_store import KeyStoreProducer, fingerprint
    from jiuwen_memory.common.security.types import Role

    register_plugins()
    keys = KeyStoreProducer.build("memory", {}, AssemblyContext())
    token = keys.issue(ALICE, Role.USER)
    data = configuration(tmp_path)
    data["authenticator"] = {
        "default": {"target": "api_key", "params": {"key_store": "default"}}
    }
    data["key_store"] = {"default": {"target": "maintenance_test_keys"}}
    raw = seed(tmp_path)

    def revoked_records():
        keys.revoke(fingerprint(token))
        yield raw

    with KeyStoreProducer.registration_scope():
        @KeyStoreProducer.register("maintenance_test_keys")
        def shared_keys(config):
            return keys

        result = migrate_plaintext_memories(
            revoked_records(),
            config=data,
            credentials=Credentials(api_key=token),
            operation="migrate",
        )
    assert result.status == "failed" and result.items[0].error == "authentication_failed", result
    assert not result.items[0].committed
    assert value(tmp_path) == raw


def test_failed_maintenance_assembly_closes_new_stores_without_mutating(tmp_path, monkeypatch):
    from jiuwen_memory.common.security.runtime import SecurityRuntime

    raw = seed(tmp_path)
    closed = []
    close = SQLiteKVStore.close

    def unhealthy(self):
        raise BackendError("security unhealthy")

    def observed_close(self):
        closed.append(id(self))
        return close(self)

    monkeypatch.setattr(SecurityRuntime, "health", unhealthy)
    monkeypatch.setattr(SQLiteKVStore, "close", observed_close)
    result = migrate_plaintext_memories(
        config=configuration(tmp_path),
        credentials=Credentials(api_key="alice-token"),
        operation="migrate",
    )
    assert result.status == "failed" and result.error == "operation_failed", result
    assert len(closed) == 1, "failed assembly must close its new KV store"
    assert value(tmp_path) == raw


def test_maintenance_requires_explicit_security_configuration(tmp_path):
    raw = seed(tmp_path)
    data = configuration(tmp_path)
    data.pop("security")
    result = migrate_plaintext_memories(
        config=data, credentials=Credentials(api_key="alice-token"), operation="clear"
    )
    assert result.status == "failed" and result.error == "authentication_failed", result
    assert value(tmp_path) == raw


@pytest.mark.parametrize("same_store", [True, False])
def test_maintenance_keeps_configured_audit_integrity_binding(tmp_path, monkeypatch, same_store):
    from jiuwen_memory.common.audit.protected_audit_logger import ProtectedAuditLogger

    raw = seed(tmp_path)
    data = configuration(tmp_path)
    data["key_provider"]["audit_keys"] = {"target": "local", "params": {"key_hex": "22" * 32}}
    data["audit"] = {
        name: {"target": "sqlite", "params": {"db_path": ":memory:"}}
        for name in ("default", "other")
    }
    data["audit_integrity"] = {
        "default": {
            "target": "chained_hmac",
            "params": {"key_provider": "audit_keys", "audit": "default" if same_store else "other"},
        }
    }
    data["security"]["default"]["params"]["audit_integrity"] = "default"
    data["security"]["default"]["params"]["allow_test_only_security"] = True
    events = []
    record = ProtectedAuditLogger.record

    def observed_record(self, event):
        events.append(event)
        return record(self, event)

    monkeypatch.setattr(ProtectedAuditLogger, "record", observed_record)
    result = migrate_plaintext_memories(
        config=data, credentials=Credentials(api_key="alice-token"), operation="migrate"
    )
    if same_store:
        assert result.status == "complete", result
        assert len(events) == 2
        assert all(event.actor == ALICE and event.action == "migrate_memory" for event in events)
        assert value(tmp_path).startswith(b"ENC1")
    else:
        assert result.status == "failed" and result.error == "invalid_input", result
        assert events == []
        assert value(tmp_path) == raw
