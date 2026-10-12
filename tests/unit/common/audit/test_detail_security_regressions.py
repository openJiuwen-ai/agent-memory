"""受保护审计的敏感字段、标识白名单及大小边界回归。"""

import json

import pytest

from jiuwen_memory.common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
from jiuwen_memory.common.audit.protected_audit_logger import ProtectedAuditLogger
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    ChainedHmacAuditIntegrityProvider,
)
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalKeyProvider,
)
from jiuwen_memory.common.type_def import AuditEvent

pytestmark = pytest.mark.unit


@pytest.fixture(name="chain_factory")
def _chain_factory_fixture(tmp_path):
    stores = []

    def make(*, retries=10, key_retries=10):
        store = SqliteAuditLogger(str(tmp_path / f"audit-{len(stores)}.sqlite3"))
        stores.append(store)
        keys = LocalKeyProvider(key_hex="44" * 32)
        provider = ChainedHmacAuditIntegrityProvider(
            store, keys, max_cas_retries=retries, max_key_retries=key_retries
        )
        return store, keys, provider, ProtectedAuditLogger(provider, store)

    yield make
    for store in stores:
        store.close()


@pytest.mark.parametrize("name", ["content", "headers", "root_key", "apiKey"])
def test_detail_forbidden_payload_does_not_reach_sqlite(chain_factory, name):
    store, keys, provider, wrapper = chain_factory()
    secret = "SYNTHETIC_PRIVATE_PAYLOAD_DO_NOT_STORE"
    wrapper.record(AuditEvent(id="probe", action="write", detail={name: secret}))
    stored = store.query({"action": "write"}, 10)[0]
    assert secret not in json.dumps(stored.detail), stored.detail


def test_detail_key_length_is_bounded(chain_factory):
    store, keys, provider, wrapper = chain_factory()
    wrapper.record(AuditEvent(id="probe", action="write", detail={"x" * 1_000_000: "v"}))
    stored = store.query({"action": "write"}, 10)[0]
    assert len(json.dumps(stored.detail)) < 100_000, (
        "one key bypasses the advertised detail size bound"
    )


@pytest.mark.parametrize("key", ["audit_key", "x" * 256 + "password"])
def test_forbidden_detail_survives_neither_new_name_nor_key_truncation(chain_factory, key):
    store, keys, provider, wrapper = chain_factory()
    value = "SYNTHETIC_AUDIT_SECRET_NEVER_PERSIST"
    wrapper.record(AuditEvent(id="probe", action="write", detail={key: value}))
    stored = store.query({"action": "write"}, 10)[0]
    assert value not in json.dumps(stored.detail), stored.detail


def test_kdf_material_does_not_enter_protected_audit(chain_factory):
    store, keys, provider, wrapper = chain_factory()
    value = "SYNTHETIC_KDF_INTERMEDIATE_MUST_NOT_PERSIST"
    wrapper.record(AuditEvent(action="write", detail={"kdf_output": value}))
    stored = store.query({"action": "write"}, 10)[0]
    assert value not in json.dumps(stored.detail), stored.detail


def test_nonsecret_credential_identifier_remains_traceable(chain_factory):
    store, keys, provider, wrapper = chain_factory()
    wrapper.record(AuditEvent(action="authenticate", detail={"credential_id": "cred-id-123"}))
    stored = store.query({"action": "authenticate"}, 10)[0]
    assert stored.detail.get("credential_id") == "cred-id-123", stored.detail


def _write_and_read(tmp_path, detail):
    store = SqliteAuditLogger(str(tmp_path / "audit.sqlite3"))
    try:
        keys = LocalKeyProvider(key_hex="44" * 32)
        provider = ChainedHmacAuditIntegrityProvider(store, keys)
        ProtectedAuditLogger(provider, store).record(AuditEvent(action="probe", detail=detail))
        return store.query({"action": "probe"}, 10)[0].detail
    finally:
        store.close()


@pytest.mark.parametrize("key", ["credential_id", "key_fp"])
def test_declared_nonsecret_identifiers_survive(tmp_path, key):
    value = "public-identifier-123"
    assert _write_and_read(tmp_path, {key: value})[key] == value


@pytest.mark.parametrize("key", ["raw_password_id", "kdf_output_fp"])
def test_unknown_sensitive_extensions_are_not_approved_by_suffix(tmp_path, key):
    # U2 要求明确的服务器字段/扩展规则。这里提供的是未批准扩展字段中的合成秘密，
    # 不是合法 credential_id/key_fp。不能只靠字段最后几个字符将它归为非秘密。
    value = "SYNTHETIC_SECRET_MATERIAL_NOT_AN_IDENTIFIER"
    stored = _write_and_read(tmp_path, {key: value})
    assert value not in json.dumps(stored), stored
