"""Full assembly: public memory API always sees protected truth, human proof is separate."""

import copy
import hashlib
from dataclasses import asdict

import pytest

from jiuwen_memory.api import (
    Context,
    DeleteMode,
    DeleteSelector,
    MemoryPatch,
    Scope,
    ValidationError,
    assemble_server_runtime,
    internal_context,
)
from jiuwen_memory.common.security.types import Role
from tests.support.scoped_authenticator import ScopedAuthenticator
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.integration
SCOPE = Scope(org="o", user="alice")
INVITE = "trusted-out-of-band-invitation-" + "a" * 32
TEXT = "联系我：13800138000，api_key=private-demo-value"


def config(tmp_path, *, enabled=True, human=False, origin="http://localhost"):
    settings = {
        "globals": {"privacy_enabled": enabled, "vector_enabled": False},
        "audit": {"default": "in_memory"},
        "permission": {"default": "allow_all"},
        "privacy": {
            "default": {
                "target": "regex",
                "params": {
                    "vault_path": str(tmp_path / "private.sqlite"),
                    "cryptography": "private",
                },
            }
        },
        "cryptography": {"private": {"target": "local", "params": {"key_provider": "private"}}},
        "key_provider": {"private": {"target": "local", "params": {"key_hex": "11" * 32}}},
    }
    if human:
        settings["human_authenticator"] = {
            "default": {
                "target": "passkey",
                "params": {
                    "origin": origin,
                    "rp_id": "localhost",
                    "credential_path": str(tmp_path / "human.sqlite"),
                    "enrollment_invites": [
                        {
                            "digest": hashlib.sha256(INVITE.encode()).hexdigest(),
                            "actor": asdict(SCOPE),
                        }
                    ],
                },
            }
        }
    return settings


@pytest.fixture
def runtime(tmp_path):
    result = assemble_server_runtime(config=config(tmp_path, human=True))
    yield result
    result.close()


def security(scope=SCOPE):
    return internal_context(ScopedAuthenticator(scope, role=Role.USER))


def test_add_recall_update_and_purge_never_restore_for_machine(runtime):
    api, ctx = runtime.api, security()
    unit = api.add(TEXT, SCOPE, security=ctx)[0]
    assert "private-demo-value" not in unit.content and "13800138000" not in unit.content
    assert api.get(unit.id, SCOPE, security=ctx).content == unit.content
    recalled = api.search("联系我", Context(scope=SCOPE), security=ctx)
    assert "private-demo-value" not in str(recalled)
    updated = api.update(unit.id, SCOPE, MemoryPatch(content="新电话13800138001"), security=ctx)
    assert "13800138001" not in updated.content
    assert updated.system_metadata["privacy_revision"]
    api.delete(
        DeleteSelector(scope=SCOPE, unit_ids=[updated.id], mode=DeleteMode.PURGE), security=ctx
    )
    events = runtime.audit.query({})
    assert any(event.action == "privacy_protection" for event in events)
    assert "private-demo-value" not in str(events)


def test_human_view_restores_complete_sentence_and_never_changes_memory(runtime):
    unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
    target = {
        "scope": asdict(SCOPE),
        "unit_id": unit.id,
        "revision": unit.system_metadata["privacy_revision"],
    }
    view, key = runtime.human_view, VirtualPasskey()
    enroll = view.begin({**target, "invite": INVITE}, "browser")
    assert view.finish(
        {
            **target,
            "challenge_id": enroll["challenge_id"],
            "credential": key.response(enroll, enroll=True),
        },
        "browser",
    )["enrolled"]
    begin = view.begin(target, "browser")
    result = view.finish(
        {
            **target,
            "challenge_id": begin["challenge_id"],
            "credential": key.response(begin, counter=2),
        },
        "browser",
    )
    assert result["content"] == TEXT
    assert runtime.api.get(unit.id, SCOPE, security=security()).content == unit.content
    assert "private-demo-value" not in str(
        runtime.api.search("联系", Context(scope=SCOPE), security=security())
    )


def test_changed_memory_requires_another_human_verification(runtime):
    unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
    view, key = runtime.human_view, VirtualPasskey()
    target = {
        "scope": asdict(SCOPE),
        "unit_id": unit.id,
        "revision": unit.system_metadata["privacy_revision"],
    }
    start = view.begin({**target, "invite": INVITE}, "browser")
    view.finish(
        {
            **target,
            "challenge_id": start["challenge_id"],
            "credential": key.response(start, enroll=True),
        },
        "browser",
    )
    start = view.begin(target, "browser")
    from jiuwen_memory.api import UpdateMode

    runtime.api.update(
        unit.id,
        SCOPE,
        MemoryPatch(content="新电话13800138001", mode=UpdateMode.OVERWRITE),
        security=security(),
    )
    with pytest.raises(ValidationError, match="changed"):
        view.finish(
            {
                **target,
                "challenge_id": start["challenge_id"],
                "credential": key.response(start, counter=2),
            },
            "browser",
        )


def test_total_switch_preserves_config_and_existing_placeholders(tmp_path):
    settings = config(tmp_path, enabled=False, human=True)
    before = copy.deepcopy(settings)
    runtime = assemble_server_runtime(config=settings)
    try:
        assert runtime.human_view is None
        unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
        assert unit.content == TEXT
        masked = "已有[JW-PRIVATE:" + "a" * 48 + "]"
        assert runtime.api.add(masked, SCOPE, security=security())[0].content == masked
        assert settings == before
        assert not (tmp_path / "private.sqlite").exists()
    finally:
        runtime.close()


def test_enabled_without_strategy_and_non_boolean_switch_are_rejected():
    for enabled in (True, "false", 0, 1, [], {}):
        with pytest.raises(ValidationError):
            assemble_server_runtime(config={"globals": {"privacy_enabled": enabled}})


@pytest.mark.parametrize("engine", ["in_memory", "cloud", "agent"])
def test_all_builtin_engines_keep_batch_inputs_and_metadata_protected(tmp_path, engine):
    from jiuwen_memory.api import BatchWriteItem

    settings = config(tmp_path)
    dependencies = ("ingestor", "index_builder", "retriever", "scheduler", "evolver", "lifecycle")
    settings["engine"] = {
        "default": {
            "target": engine,
            "params": dict.fromkeys(dependencies, "default"),
        }
    }
    runtime = assemble_server_runtime(config=settings)
    try:
        result = runtime.api.batch_add(
            [
                BatchWriteItem(
                    content=TEXT,
                    scope=SCOPE,
                    tags=["13800138000"],
                    user_metadata={"api_key": "short", "password": "a secret phrase"},
                )
            ],
            security=security(),
        )
        assert not result.outcomes[0].error
        assert result.outcomes[0].units
        for secret in ("private-demo-value", "13800138000", "short", "a secret phrase"):
            assert secret not in str(result)
        unit = result.outcomes[0].units[0]
        assert runtime.api.get(unit.id, SCOPE, security=security()).content == unit.content
    finally:
        runtime.close()


def test_disabling_after_restart_keeps_existing_truth_masked(tmp_path):
    settings = config(tmp_path)
    settings["kv_store"] = {
        "default": {
            "target": "sqlite",
            "params": {
                "db_path": str(tmp_path / "memory.sqlite"),
            },
        }
    }
    runtime = assemble_server_runtime(config=settings)
    unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
    runtime.close()
    settings["globals"]["privacy_enabled"] = False
    restarted = assemble_server_runtime(config=settings)
    try:
        assert restarted.human_view is None
        assert restarted.api.get(unit.id, SCOPE, security=security()).content == unit.content
        assert "private-demo-value" not in unit.content
    finally:
        restarted.close()


def test_human_proof_does_not_replace_resource_authorization(runtime):
    from jiuwen_memory.api import PermissionDeniedError

    victim_scope = Scope(org="o", user="bob")
    victim = runtime.api.add(TEXT, victim_scope, security=security(victim_scope))[0]
    target = {
        "scope": asdict(victim_scope),
        "unit_id": victim.id,
        "revision": victim.system_metadata["privacy_revision"],
    }
    view, key = runtime.human_view, VirtualPasskey()
    start = view.begin({**target, "invite": INVITE}, "browser")
    view.finish(
        {
            **target,
            "challenge_id": start["challenge_id"],
            "credential": key.response(start, enroll=True),
        },
        "browser",
    )
    start = view.begin(target, "browser")
    with pytest.raises(PermissionDeniedError):
        view.finish(
            {
                **target,
                "challenge_id": start["challenge_id"],
                "credential": key.response(start, counter=2),
            },
            "browser",
        )


def test_existing_plaintext_namespace_is_rejected_before_runtime_publication(tmp_path):
    settings = config(tmp_path, enabled=False)
    settings["kv_store"] = {
        "default": {
            "target": "sqlite",
            "params": {
                "db_path": str(tmp_path / "old-memory.sqlite"),
            },
        }
    }
    old = assemble_server_runtime(config=settings)
    old.api.add(TEXT, SCOPE, security=security())
    old.close()
    settings["globals"]["privacy_enabled"] = True
    with pytest.raises(ValidationError, match="migration"):
        assemble_server_runtime(config=settings)


def test_privacy_events_use_existing_integrity_protected_audit(tmp_path):
    from jiuwen_memory.common.security.audit_integrity.base import AuditIntegrityStatus

    settings = config(tmp_path)
    settings["audit"] = {
        "default": {
            "target": "sqlite",
            "params": {
                "db_path": str(tmp_path / "audit.sqlite"),
            },
        }
    }
    settings["key_provider"]["audit"] = {"target": "local", "params": {"key_hex": "44" * 32}}
    settings["audit_integrity"] = {
        "default": {
            "target": "chained_hmac",
            "params": {
                "key_provider": "audit",
                "audit": "default",
            },
        }
    }
    settings["security"] = {
        "default": {
            "target": "standard",
            "params": {
                "authenticator": {"target": "dev"},
                "authorizer": "default",
                "audit_integrity": "default",
            },
        }
    }
    runtime = assemble_server_runtime(config=settings)
    try:
        runtime.api.add(TEXT, SCOPE, security=security())
        root = internal_context(
            ScopedAuthenticator(Scope(org="system", user="operator"), role=Role.ROOT)
        )
        assert runtime.api.verify_audit(security=root).status is AuditIntegrityStatus.CLEAN
        events = runtime.audit.query({})
        assert any(event.action == "privacy_protection" for event in events)
        assert "private-demo-value" not in str(events)
    finally:
        runtime.close()


def test_privacy_can_compose_with_existing_encrypted_kv(tmp_path):
    settings = config(tmp_path)
    settings["kv_store"] = {
        "raw": {"target": "memory"},
        "default": {
            "target": "encrypted",
            "params": {
                "raw_kv_store": "raw",
                "cryptography": "private",
            },
        },
    }
    runtime = assemble_server_runtime(config=settings)
    try:
        unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
        assert "private-demo-value" not in unit.content
        assert runtime.api.get(unit.id, SCOPE, security=security()).content == unit.content
        unit = runtime.api.update(
            unit.id, SCOPE, MemoryPatch(content="新电话13800138001"), security=security()
        )
        assert "13800138001" not in unit.content
        runtime.api.delete(
            DeleteSelector(scope=SCOPE, unit_ids=[unit.id], mode=DeleteMode.PURGE),
            security=security(),
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("transition", ["supersede", "forget", "archive"])
def test_inactive_memory_cannot_release_plaintext(runtime, transition):
    unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
    target = {
        "scope": asdict(SCOPE),
        "unit_id": unit.id,
        "revision": unit.system_metadata["privacy_revision"],
    }
    view, key = runtime.human_view, VirtualPasskey()
    enroll = view.begin({**target, "invite": INVITE}, "browser")
    view.finish(
        {
            **target,
            "challenge_id": enroll["challenge_id"],
            "credential": key.response(enroll, enroll=True),
        },
        "browser",
    )
    start = view.begin(target, "browser")
    if transition == "supersede":
        runtime.api.update(
            unit.id, SCOPE, MemoryPatch(content="新电话13800138001"), security=security()
        )
    else:
        mode = DeleteMode.FORGET if transition == "forget" else DeleteMode.ARCHIVE
        runtime.api.delete(
            DeleteSelector(scope=SCOPE, unit_ids=[unit.id], mode=mode), security=security()
        )
    with pytest.raises(ValidationError, match="not active"):
        view.finish(
            {
                **target,
                "challenge_id": start["challenge_id"],
                "credential": key.response(start, counter=2),
            },
            "browser",
        )


def test_completed_api_write_does_not_keep_volatile_original_fragments(runtime):
    from jiuwen_memory.common.security.privacy.base import PrivacyProtection
    from tests.support.privacy import assert_staging_unavailable

    unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
    dependencies = runtime.human_view.security_dependencies()
    privacy = next(item for item in dependencies if isinstance(item, PrivacyProtection))
    assert_staging_unavailable(privacy, unit)
    assert runtime.api.get(unit.id, SCOPE, security=security()).content == unit.content


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
def test_single_and_batch_add_preserve_sensitive_tags_and_classification(
    tmp_path, monkeypatch, engine, privacy_state
):
    from jiuwen_memory.api import BatchWriteItem
    from jiuwen_memory.common.security.privacy.base import PRIVACY_TOKEN
    from jiuwen_memory.construction.classifier_impl.llm_classifier import LLMClassifier

    settings = config(tmp_path)
    settings["engine"] = {"default": {"target": engine, "params": {"index_builder": "default"}}}
    runtime = assemble_server_runtime(config=settings)
    try:
        classified = []

        def classify(self, units):
            for unit in units:
                assert unit.system_metadata["privacy_level"] == "S3"
                assert all(PRIVACY_TOKEN.search(tag) for tag in unit.tags)
                classified.append(unit.id)
            return units

        monkeypatch.setattr(LLMClassifier, "classify", classify)
        tags = ["api_key=tag-secret", "13800138000"]
        single = runtime.api.add("公开正文单条", SCOPE, tags=tags, security=security())[0]
        batch = (
            runtime.api.batch_add(
                [BatchWriteItem(content="公开正文批量", tags=tags)], SCOPE, security=security()
            )
            .outcomes[0]
            .units[0]
        )
        for unit in (single, batch):
            assert unit.system_metadata["privacy_level"] == "S3"
            assert all(PRIVACY_TOKEN.search(tag) for tag in unit.tags)
            assert "tag-secret" not in str(unit) and "13800138000" not in str(unit)
            assert (
                privacy_state.execute(
                    "SELECT COUNT(*) FROM bindings WHERE unit=?", ("memory/" + unit.id,)
                ).fetchone()[0]
                == 2
            )
        assert len(classified) == 2
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "failure", ["duplicate", "update", "delete", "index_update", "index_delete", "after_truth"]
)
def test_surviving_memory_can_be_viewed_with_fresh_human_proof(runtime, monkeypatch, failure):
    from jiuwen_memory.api import UpdateMode
    from jiuwen_memory.common.errors import BackendError, ConflictError
    from jiuwen_memory.common.type_def.memory import memory_key
    from jiuwen_memory.construction.index_builder_impl.fulltext_index_builder import (
        FulltextIndexBuilder,
    )
    from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import (
        HybridIndexBuilder,
    )
    from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    api, ctx = runtime.api, security()
    old = api.add(TEXT, SCOPE, security=ctx)[0]

    def fail(*args, **kwargs):
        raise BackendError("injected mutation failure")

    if failure == "duplicate":
        ingest = SimpleIngestor.ingest

        def repeat_id(self, payloads):
            units = ingest(self, payloads)
            for unit in units:
                unit.id = old.id
            return units

        monkeypatch.setattr(SimpleIngestor, "ingest", repeat_id)
        with pytest.raises(ConflictError):
            api.add("新电话13800138001", SCOPE, security=ctx)
    else:
        if failure in {"update", "delete"}:
            mutate = getattr(InMemoryKVStore, failure)

            def reject_target(self, scope, key, *args, **kwargs):
                if key == memory_key(old.id):
                    raise BackendError("injected mutation failure")
                return mutate(self, scope, key, *args, **kwargs)

            monkeypatch.setattr(InMemoryKVStore, failure, reject_target)
        elif failure == "index_update":
            monkeypatch.setattr(HybridIndexBuilder, "update", fail)
        elif failure == "index_delete":
            monkeypatch.setattr(FulltextIndexBuilder, "remove", fail)
        else:
            monkeypatch.setattr(FulltextIndexBuilder, "update", fail)
        with pytest.raises(BackendError):
            if failure in {"delete", "index_delete"}:
                api.delete(
                    DeleteSelector(scope=SCOPE, unit_ids=[old.id], mode=DeleteMode.PURGE),
                    security=ctx,
                )
            else:
                api.update(
                    old.id,
                    SCOPE,
                    MemoryPatch(content="新电话13800138001", mode=UpdateMode.OVERWRITE),
                    security=ctx,
                )
    surviving = api.get(old.id, SCOPE, security=ctx)
    target = {
        "scope": asdict(SCOPE),
        "unit_id": old.id,
        "revision": surviving.system_metadata["privacy_revision"],
    }
    view, key = runtime.human_view, VirtualPasskey()
    enroll = view.begin({**target, "invite": INVITE}, "browser")
    view.finish(
        {
            **target,
            "challenge_id": enroll["challenge_id"],
            "credential": key.response(enroll, enroll=True),
        },
        "browser",
    )
    challenge = view.begin(target, "browser")
    result = view.finish(
        {
            **target,
            "challenge_id": challenge["challenge_id"],
            "credential": key.response(challenge, counter=2),
        },
        "browser",
    )
    assert result["content"] == ("新电话13800138001" if failure == "after_truth" else TEXT)


def test_privacy_composes_with_automatic_at_rest_encryption(tmp_path):
    settings = config(tmp_path)
    settings["globals"].update(
        storage_encryption_enabled=True, storage_encryption_cryptography="private"
    )
    runtime = assemble_server_runtime(config=settings)
    try:
        unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
        assert "private-demo-value" not in unit.content
        assert runtime.api.get(unit.id, SCOPE, security=security()).content == unit.content
        updated = runtime.api.update(
            unit.id, SCOPE, MemoryPatch(content="新电话13800138001"), security=security()
        )
        assert "13800138001" not in updated.content
        runtime.api.delete(
            DeleteSelector(scope=SCOPE, unit_ids=[updated.id], mode=DeleteMode.PURGE),
            security=security(),
        )
    finally:
        runtime.close()
