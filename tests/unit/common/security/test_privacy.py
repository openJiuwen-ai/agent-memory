"""Privacy behavior, real encryption, binding integrity and lifecycle regressions."""

import copy
import json
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalEnvelopeCryptographyProvider,
    LocalKeyProvider,
)
from jiuwen_memory.common.security.privacy.privacy_impl.regex import RegexPrivacy
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload, Scope, Segment
from jiuwen_memory.common.type_def.memory import ContentLayers, TreeFact, TreeMemoryData
from tests.support.privacy import assert_staging_unavailable

pytestmark = pytest.mark.unit
SCOPE = Scope(org="o", user="alice")
TEXT = "联系我：13800138000，api_key=private-demo-value"


def protected_unit(privacy, *, uid="u1", text=TEXT):
    payload = privacy.ingress(RawPayload(scope=SCOPE, data=text.encode()))
    unit = MemoryUnit(
        id=uid,
        scope=SCOPE,
        segments=[Segment(payload.data.decode())],
        source_ref=payload.id,
        system_metadata=payload.system_metadata,
    )
    privacy.persist(unit)
    return unit


def test_round_trip_preserves_sentence_without_plaintext_on_disk(privacy, tmp_path):
    raw = RawPayload(scope=SCOPE, data=TEXT.encode(), user_metadata={"phone": 13800138000})
    protected = privacy.ingress(raw)
    assert raw.data.decode() == TEXT
    assert "13800138000" not in json.dumps(protected.user_metadata)
    unit = MemoryUnit(
        id="u1",
        scope=SCOPE,
        segments=[Segment(protected.data.decode())],
        source_ref=protected.id,
        system_metadata=protected.system_metadata,
    )
    privacy.persist(unit)
    assert unit.system_metadata["privacy_level"] == "S3"
    assert "private-demo-value" not in unit.content
    assert privacy.render(unit) == TEXT
    disk = (tmp_path / "private.sqlite").read_bytes()
    assert b"13800138000" not in disk and b"private-demo-value" not in disk


def test_second_pass_covers_generated_projections(privacy):
    unit = protected_unit(privacy)
    unit.layers = ContentLayers(l0="password=generated-secret")
    unit.entities = ["13800138000"]
    unit.tags = ["13800138000"]
    unit.tree_memory = TreeMemoryData([TreeFact("source", SCOPE, "password=tree-secret")])
    privacy.persist(unit)
    assert "generated-secret" not in unit.layers.l0
    assert "13800138000" not in str(unit.entities + unit.tags)
    assert "tree-secret" not in unit.tree_memory.facts[0].text
    assert privacy.render(unit) == TEXT


@pytest.mark.parametrize("mutation", ["scope", "id", "revision", "content"])
def test_snapshot_tampering_is_rejected(privacy, mutation):
    unit = protected_unit(privacy)
    changed = copy.deepcopy(unit)
    if mutation == "scope":
        changed.scope = Scope(org="other", user="alice")
    elif mutation == "id":
        changed.id = "other"
    elif mutation == "revision":
        changed.system_metadata["privacy_revision"] = "forged"
    else:
        changed.segments[0].content += "changed"
    with pytest.raises(ValidationError):
        privacy.render(changed)


def test_overwrite_invalidates_old_view_and_reuses_unchanged_snapshot(privacy):
    unit = protected_unit(privacy)
    old = copy.deepcopy(unit)
    privacy.persist(unit)
    assert unit.system_metadata["privacy_revision"] == old.system_metadata["privacy_revision"]
    unit.segments[0].content += "，今天更新"
    privacy.persist(unit)
    with pytest.raises(ValidationError):
        privacy.render(old)
    assert privacy.render(unit) == TEXT + "，今天更新"


def test_derived_binding_survives_source_deletion_and_restart(
    privacy, privacy_state, privacy_factory
):
    source = protected_unit(privacy)
    privacy.close()
    privacy = privacy_factory()
    derived = replace(copy.deepcopy(source), id="derived", provenance=[source.id])
    privacy.persist(derived)
    privacy.discard(SCOPE, [source.id])
    assert privacy.render(derived) == TEXT
    privacy.discard(SCOPE, [derived.id])
    assert privacy_state.execute("SELECT COUNT(*) FROM fragments").fetchone()[0] == 0


def test_unrelated_unit_cannot_attach_existing_placeholder(privacy, privacy_factory):
    source = protected_unit(privacy)
    privacy.close()
    privacy = privacy_factory()
    forged = replace(source, id="unrelated", source_ref="", provenance=[])
    with pytest.raises(ValidationError, match="unbound"):
        privacy.persist(forged)


def test_message_eviction_keeps_truth_binding(privacy):
    unit = protected_unit(privacy)
    privacy.persist(unit, channel="message")
    privacy.discard(SCOPE, [unit.id], channel="message")
    assert privacy.render(unit) == TEXT


def test_scope_delete_cleans_private_records_and_staging(privacy, privacy_state):
    unit = protected_unit(privacy)
    privacy.discard_scope(Scope(org="o"))
    with pytest.raises(ValidationError):
        privacy.render(unit)
    assert_staging_unavailable(privacy, unit)
    assert privacy_state.execute("SELECT COUNT(*) FROM fragments").fetchone()[0] == 0


def test_client_placeholder_is_not_a_disclosure_capability(privacy):
    unit = protected_unit(privacy)
    with pytest.raises(ValidationError, match="reserved"):
        privacy.ingress(RawPayload(scope=SCOPE, data=unit.content.encode()))
    forged = MemoryUnit(id="bad", scope=SCOPE, segments=[Segment("[JW-PRIVATE:bad]")])
    with pytest.raises(ValidationError, match="malformed"):
        privacy.persist(forged)


def test_custom_level_allow_and_reveal_are_independent(tmp_path):
    privacy = RegexPrivacy(
        LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_hex="11" * 32)),
        str(tmp_path / "custom.sqlite"),
        levels=[
            {"name": "public", "rank": 0, "write": "allow"},
            {"name": "pii", "rank": 1, "write": "allow", "reveal": False},
            {"name": "credential", "rank": 2, "write": "redact", "reveal": False},
        ],
        rules=[{"level": "pii", "pattern": "phone"}, {"level": "credential", "pattern": "secret"}],
    )
    try:
        unit = protected_unit(privacy, text="phone and secret")
        assert "phone" in unit.content and "secret" not in unit.content
        assert unit.system_metadata["privacy_level"] == "credential"
        assert privacy.render(unit) == unit.content
    finally:
        privacy.close()


def test_staging_expiry_fails_closed(privacy_factory, monkeypatch):
    privacy = privacy_factory(staging_ttl=1)
    payload = privacy.ingress(RawPayload(scope=SCOPE, data=TEXT.encode()))
    import time

    expired = time.monotonic() + 2
    monkeypatch.setattr(
        "jiuwen_memory.common.security.privacy.privacy_impl.regex.time.monotonic", lambda: expired
    )
    with pytest.raises(ValidationError, match="expired"):
        privacy.persist(
            MemoryUnit(
                id="expired",
                scope=SCOPE,
                source_ref=payload.id,
                segments=[Segment(payload.data.decode())],
            )
        )


def test_trusted_routing_copies_ciphertext_without_decrypting_fragments(
    privacy, monkeypatch, privacy_crypto, privacy_factory
):
    source = protected_unit(privacy)
    privacy.close()
    privacy = privacy_factory()
    derived = replace(copy.deepcopy(source), id="derived", provenance=[source.id])
    purposes = []
    decrypt = privacy_crypto.decrypt

    def observe(cipher, *, context, aad=b""):
        purposes.append(context.purpose)
        return decrypt(cipher, context=context, aad=aad)

    monkeypatch.setattr(privacy_crypto, "decrypt", observe)
    target = Scope(org="o", user="alice", space="project")
    privacy.relocate(derived, target)
    derived.scope = target
    privacy.persist(derived)
    assert "privacy-fragment" not in purposes
    privacy.discard(SCOPE, [source.id])
    assert privacy.render(derived) == TEXT
    assert "privacy-fragment" in purposes


def test_scope_change_without_trusted_relocation_fails_closed(privacy):
    source = protected_unit(privacy)
    derived = replace(
        copy.deepcopy(source),
        id="derived",
        provenance=[source.id],
        scope=Scope(org="o", user="bob"),
    )
    with pytest.raises(ValidationError, match="unbound"):
        privacy.persist(derived)
    with pytest.raises(ValidationError, match="organizations"):
        privacy.relocate(source, Scope(org="other", user="alice"))


def test_tampered_reference_table_cannot_authorize_a_fragment(
    privacy, privacy_state, privacy_factory
):
    victim = protected_unit(privacy)
    protected_unit(privacy, uid="other", text="另一个电话号码13800138002")
    privacy.close()
    privacy = privacy_factory()
    with privacy_state:
        privacy_state.execute(
            "INSERT OR IGNORE INTO bindings SELECT scope,?,token FROM bindings WHERE unit=?",
            ("memory/other", "memory/u1"),
        )
    forged = replace(copy.deepcopy(victim), id="forged", source_ref="", provenance=["other"])
    with pytest.raises(ValidationError, match="unbound"):
        privacy.persist(forged)


def test_ingress_protects_before_processing_and_retains_original_level(privacy, privacy_state):
    from jiuwen_memory.ingest.ingestor import Ingestor
    from jiuwen_memory.ingest.privacy_ingestor import PrivacyIngestor

    class SummarizingIngestor(Ingestor):
        def operator_type(self):
            return "ingestor"

        def health(self):
            return None

        def ingest(self, payloads):
            assert b"private-demo-value" not in payloads[0].data
            assert b"13800138000" not in payloads[0].data
            return [
                MemoryUnit(
                    id="summary",
                    scope=payloads[0].scope,
                    source_ref=payloads[0].id,
                    segments=[Segment("经过处理的摘要")],
                )
            ]

    unit = PrivacyIngestor(SummarizingIngestor(), privacy).ingest(
        [
            RawPayload(scope=SCOPE, data=TEXT.encode()),
        ]
    )[0]
    assert unit.system_metadata["privacy_level"] == "S3"
    assert privacy_state.execute("SELECT COUNT(*) FROM manifests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text,secret",
    [
        ("我的身份证110101199001011234号", "110101199001011234"),
        ("邮箱alice@example.com请联系", "alice@example.com"),
        ("密钥sk-abcdefghijklmnopqrst", "sk-abcdefghijklmnopqrst"),
        ('{"api_key":"xy"}', "xy"),
        ('{"password":"a secret phrase"}', "a secret phrase"),
        ("password='a secret phrase'", "a secret phrase"),
        (
            "-----BEGIN PRIVATE KEY-----\nprivatepayload\n-----END PRIVATE KEY-----",
            "privatepayload",
        ),
    ],
)
def test_default_rules_cover_chinese_context_and_quoted_credentials(privacy, text, secret):
    unit = protected_unit(privacy, text=text)
    assert secret not in unit.content
    assert privacy.render(unit) == text


def test_metadata_field_context_and_batch_echo_are_protected(privacy):
    metadata = {"api_key": "xy", "password": "a secret phrase", "phone": 13800138000}
    result = privacy.ingress(RawPayload(scope=SCOPE, user_metadata=metadata))
    for value in ("xy", "a secret phrase", "13800138000"):
        assert value not in str(result.user_metadata)
        assert value not in str(privacy.preview_echo(metadata))


def test_original_source_policy_controls_are_retained_as_todo(tmp_path):
    policy = {
        "name": "custom",
        "rank": 0,
        "write": "redact",
        "label": "自定义",
        "sync_mode": "index_only",
        "share_mode": "internal_only",
        "retention_mode": "purge_after_ttl",
        "retention_seconds": 86400,
    }
    privacy = RegexPrivacy(
        LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_hex="11" * 32)),
        str(tmp_path / "todo.sqlite"),
        levels=[policy],
        rules=[],
    )
    try:
        assert privacy.levels["custom"].retention_seconds == 86400
        assert privacy.levels["custom"].sync_mode == "index_only"
        assert privacy.levels["custom"].share_mode == "internal_only"
    finally:
        privacy.close()


def test_detection_capacity_rejects_before_staging(privacy_factory):
    privacy = privacy_factory(staging_limit=2)
    with pytest.raises(ValidationError, match="detection capacity"):
        privacy.ingress(RawPayload(scope=SCOPE, data=("13800138000 " * 50).encode()))
    for phone in ("13800138001", "13800138002"):
        payload = privacy.ingress(RawPayload(scope=SCOPE, data=phone.encode()))
        assert phone.encode() not in payload.data


@pytest.mark.parametrize("group", [True, None, [], -1, "missing"])
def test_invalid_regex_group_is_a_configuration_error(tmp_path, group):
    crypto = LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_hex="11" * 32))
    with pytest.raises(ValidationError):
        RegexPrivacy(
            crypto,
            str(tmp_path / "invalid.sqlite"),
            rules=[{"level": "S2", "pattern": "(phone)", "group": group}],
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", 1),
        ("label", 1),
        ("description", None),
        ("rank", True),
        ("rank", -1),
        ("rank", 1.0),
        ("reveal", 1),
        ("reveal", "false"),
        ("retention_seconds", True),
        ("retention_seconds", -1),
        ("write", "unknown"),
        ("sync_mode", "unknown"),
        ("retention_mode", "unknown"),
        ("share_mode", "unknown"),
    ],
)
def test_level_validation_preserves_strict_types_and_policy_modes(privacy_factory, field, value):
    level = {"name": "private", "rank": 1, field: value}
    with pytest.raises(ValidationError, match="invalid privacy levels"):
        privacy_factory(levels=[level])


@pytest.mark.parametrize("timeout", [True, False, "2", 0, 31])
def test_regex_timeout_configuration_rejects_booleans_and_invalid_ranges(privacy_factory, timeout):
    with pytest.raises(ValidationError, match="invalid privacy levels"):
        privacy_factory(regex_timeout=timeout)


def test_generated_secrets_adjacent_to_valid_tokens_are_also_masked(privacy):
    from jiuwen_memory.common.security.privacy.privacy_impl.regex import TOKEN

    source = protected_unit(privacy, text="api_key=original-secret")
    token = TOKEN.search(source.content).group()
    value = "generated-prefix" + token + "generated-suffix"
    derived = MemoryUnit(
        id="derived",
        scope=SCOPE,
        provenance=[source.id],
        segments=[Segment("api_key=" + value)],
        user_metadata={"api_key": value},
    )
    privacy.persist(derived)
    assert "generated-prefix" not in derived.content
    assert "generated-suffix" not in derived.content
    assert "generated-prefix" not in str(derived.user_metadata)
    assert privacy.render(derived) == "api_key=generated-prefixoriginal-secretgenerated-suffix"


def test_credential_rule_cannot_hide_a_malformed_placeholder(privacy):
    unit = MemoryUnit(id="broken", scope=SCOPE, segments=[Segment("api_key=[JW-PRIVATE:broken]")])
    with pytest.raises(ValidationError, match="malformed"):
        privacy.persist(unit)


def test_custom_regex_timeout_releases_already_staged_ciphertext(tmp_path):
    privacy = RegexPrivacy(
        LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_hex="11" * 32)),
        str(tmp_path / "bounded.sqlite"),
        regex_timeout=0.01,
        staging_limit=1,
        rules=[
            {"level": "S2", "pattern": r"1[3-9]\d{9}"},
            {"level": "S3", "pattern": "(a+)+$"},
        ],
    )
    try:
        with pytest.raises(ValidationError, match="regex timeout"):
            privacy.ingress(
                RawPayload(
                    scope=SCOPE,
                    user_metadata={
                        "phone": "13800138000",
                        "attack": "a" * 20000 + "!",
                    },
                )
            )
        recovered = privacy.ingress(RawPayload(scope=SCOPE, data=b"13800138001"))
        assert b"13800138001" not in recovered.data
    finally:
        privacy.close()


def test_staging_scope_preserves_fanout_until_all_units_commit(privacy):
    with privacy.staging_scope():
        payload = privacy.ingress(RawPayload(scope=SCOPE, data=TEXT.encode()))
        units = [
            MemoryUnit(
                id=f"fanout-{index}",
                scope=SCOPE,
                source_ref=payload.id,
                segments=[Segment(payload.data.decode())],
            )
            for index in range(2)
        ]
        for unit in units:
            privacy.persist(unit)
    assert_staging_unavailable(privacy, payload)
    assert all(privacy.render(unit) == TEXT for unit in units)


def test_late_callback_cannot_stage_after_its_operation_closed(privacy_factory):
    from contextvars import copy_context

    privacy = privacy_factory(staging_limit=2)
    with privacy.staging_scope():
        callback_context = copy_context()
    with pytest.raises(ValidationError, match="closed"):
        callback_context.run(privacy.ingress, RawPayload(scope=SCOPE, data=TEXT.encode()))
    fresh = privacy.ingress(RawPayload(scope=SCOPE, data=TEXT.encode()))
    assert b"private-demo-value" not in fresh.data


def test_ingest_hard_cut_preserves_complete_private_references(privacy):
    from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
        PassthroughNormalizer,
    )
    from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor
    from jiuwen_memory.ingest.privacy_ingestor import PrivacyIngestor

    text = "x" * 90 + TEXT + "z" * 150
    ingestor = PrivacyIngestor(SimpleIngestor(PassthroughNormalizer(), max_unit_chars=100), privacy)
    units = ingestor.ingest([RawPayload(scope=SCOPE, data=text.encode())])
    assert len(units) > 1 and all(len(unit.content) <= 100 for unit in units)
    for unit in units:
        privacy.persist(unit)
    assert "".join(privacy.render(unit) for unit in units) == text


def test_content_prompt_chunks_preserve_private_references(privacy):
    from jiuwen_memory.construction.prompt_guard import chunk_unit

    text = "x" * 90 + TEXT + "z" * 150
    unit = protected_unit(privacy, text=text)
    chunks = chunk_unit(unit, limit=100)
    for part in chunks:
        privacy.prepare(part)
    assert all(len(part.content) <= 100 for part in chunks)
    assert "".join(part.content for part in chunks) == unit.content


def test_privacy_rejects_a_split_limit_smaller_than_a_reference(privacy):
    from jiuwen_memory.construction.prompt_guard import chunk_unit

    unit = protected_unit(privacy, text="13800138000" + "z" * 100)
    with pytest.raises(ValidationError, match="limit"):
        chunk_unit(unit, limit=10)


@pytest.mark.parametrize(("offset", "suffix"), [(50, 150), (70, 150), (80, 10)])
def test_decision_prompt_truncation_keeps_private_references_whole(privacy, offset, suffix):
    from jiuwen_memory.construction.prompt_guard import truncate_unit_content

    unit = protected_unit(privacy, text="x" * offset + "13800138000" + "z" * suffix)
    bounded = truncate_unit_content(unit.content, limit=100)
    assert "13800138000" not in bounded
    privacy.preview(bounded)  # Rejects a truncated reserved placeholder prefix.
    assert "PRIVATE:" not in bounded  # The crossing reference is omitted in full.
