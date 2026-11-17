"""真实 MemoryAPI/隐私装配→插件最小视图→可信来源状态，不访问内核私有端口。"""

import asyncio
from dataclasses import asdict, replace

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_adapter.agent_security import Actor, SecurityDenied
from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector
from jiuwen_memory_adapter.agent_security.memory import SdkMemoryReader
from jiuwen_memory_adapter.agent_security.sources import SourceLedger

from .fixtures import sdk_security, sdk_settings

pytestmark = pytest.mark.integration
ACTOR = Actor("o", "alice", "chat", "assistant", "new-context")
SCOPE = Scope(org="o", user="alice")


@pytest.fixture
def memory_runtime(tmp_path):
    from jiuwen_memory.api import assemble_server_runtime

    runtime = assemble_server_runtime(config=sdk_settings(tmp_path))
    sources = SourceLedger()
    sources.begin(ACTOR, fresh=True)
    allowed = {"value": True}
    inspector = create_text_inspector()
    reader = SdkMemoryReader(
        runtime.api,
        sources,
        inspector,
        provider="real-sdk",
        scope_for=lambda _: SCOPE,
        security_for=lambda _: sdk_security(SCOPE),
        authorize_use=lambda *args: allowed["value"],
        detector_version_for=lambda _: inspector.version,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    yield runtime, sources, reader, allowed
    reader.close()
    sources.close()
    runtime.close()


def test_real_sdk_get_and_list_only_release_safe_text_and_keep_original_level(memory_runtime):
    runtime, sources, reader, _ = memory_runtime
    unit = runtime.api.add(
        "联系 13800138000，api_key=private-demo", SCOPE, security=sdk_security(SCOPE)
    )[0]
    result = reader.get(unit.id, ACTOR)
    assert "13800138000" not in str(asdict(result))
    assert "private-demo" not in str(asdict(result))
    assert "[JW-PRIVATE:" not in result.content and "[PRIVATE]" in result.content
    assert unit.id not in result.reference
    current = sources.snapshot(ACTOR)
    assert current.level == "S3" and current.references[0].unit_id == unit.id
    listed = reader.list(ACTOR)
    assert len(listed) == 1 and listed[0] == result
    assert sources.snapshot(ACTOR).version == current.version
    assert not hasattr(reader, "human_view") and not hasattr(reader, "render")


def test_usage_denial_and_identity_mismatch_never_register_or_release(memory_runtime):
    runtime, sources, reader, allowed = memory_runtime
    unit = runtime.api.add("电话 13800138000", SCOPE, security=sdk_security(SCOPE))[0]
    allowed["value"] = False
    before = sources.snapshot(ACTOR)
    with pytest.raises(SecurityDenied):
        reader.get(unit.id, ACTOR)
    assert sources.snapshot(ACTOR) == before
    other = replace(ACTOR, user_id="bob")
    sources.begin(other, fresh=True)
    with pytest.raises(SecurityDenied):
        reader.get(unit.id, other)
    assert sources.snapshot(other).references == ()


def test_real_sdk_revision_change_or_usage_revocation_blocks_derived_egress(memory_runtime):
    from jiuwen_memory.api import MemoryPatch

    runtime, sources, reader, allowed = memory_runtime
    unit = runtime.api.add("电话 13800138000", SCOPE, security=sdk_security(SCOPE))[0]
    reader.get(unit.id, ACTOR)
    snapshot = sources.snapshot(ACTOR)
    with sources.lease(ACTOR, snapshot.version):
        with pytest.raises(SecurityDenied):
            reader.get(unit.id, ACTOR)
    allowed["value"] = False
    with pytest.raises(SecurityDenied):
        with sources.lease(ACTOR, snapshot.version):
            pytest.fail("来源撤权后不能使用记忆")
    allowed["value"] = True
    runtime.api.update(
        unit.id, SCOPE, MemoryPatch(content="改为 13900139000"), security=sdk_security(SCOPE)
    )
    with pytest.raises(SecurityDenied):
        with sources.lease(ACTOR, snapshot.version):
            pytest.fail("来源修订改变后不能继续复用旧上下文")


def test_unprotected_sdk_namespace_is_not_treated_as_public(tmp_path):
    from jiuwen_memory.api import assemble_server_runtime

    settings = sdk_settings(tmp_path)
    settings["globals"]["privacy_enabled"] = False
    runtime = assemble_server_runtime(config=settings)
    sources = SourceLedger()
    sources.begin(ACTOR, fresh=True)
    inspector = create_text_inspector()
    reader = SdkMemoryReader(
        runtime.api,
        sources,
        inspector,
        provider="unprotected",
        scope_for=lambda _: SCOPE,
        security_for=lambda _: sdk_security(SCOPE),
        authorize_use=lambda *a: True,
        detector_version_for=lambda _: inspector.version,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    try:
        unit = runtime.api.add("普通文字", SCOPE, security=sdk_security(SCOPE))[0]
        with pytest.raises(SecurityDenied):
            reader.get(unit.id, ACTOR)
        assert sources.snapshot(ACTOR).references == ()
    finally:
        reader.close()
        sources.close()
        runtime.close()


def test_real_sdk_async_search_keeps_source_before_returning_text(memory_runtime):
    runtime, sources, reader, _ = memory_runtime
    unit = runtime.api.add("orchard 联系 13800138000", SCOPE, security=sdk_security(SCOPE))[0]

    async def scenario():
        found = await reader.asearch("orchard", ACTOR)
        assert found and all("13800138000" not in view.content for view in found)
        snapshot = sources.snapshot(ACTOR)
        assert snapshot.level == "S2" and any(r.unit_id == unit.id for r in snapshot.references)

    asyncio.run(scenario())


@pytest.mark.parametrize("defect", ["unknown", "revision", "asset", "tenant"])
def test_invalid_sdk_batch_is_rejected_before_any_source_or_view_is_released(
    memory_runtime, monkeypatch, defect
):
    import copy

    from jiuwen_memory.api import MemoryListResult

    runtime, sources, reader, _ = memory_runtime
    valid = runtime.api.add("电话 13800138000", SCOPE, security=sdk_security(SCOPE))[0]
    bad = copy.deepcopy(valid)
    bad.id = "bad-result"
    if defect == "unknown":
        bad.system_metadata["privacy_level"] = "custom"
    elif defect == "revision":
        bad.system_metadata.pop("privacy_revision")
    elif defect == "asset":
        bad.segments[0].assets = ["private-attachment"]
    else:
        bad.scope.org = "other"
    monkeypatch.setattr(runtime.api, "list", lambda *a, **k: MemoryListResult([valid, bad], 2))
    original = sources.snapshot(ACTOR)
    with pytest.raises(SecurityDenied):
        reader.list(ACTOR)
    assert sources.snapshot(ACTOR) == original


def test_sdk_write_exemption_does_not_allow_plaintext_agent_delivery(tmp_path):
    from jiuwen_memory.api import assemble_server_runtime

    settings = sdk_settings(tmp_path)
    settings["privacy"]["default"]["params"]["levels"] = [
        {"name": "S1", "rank": 1, "write": "allow"},
        {"name": "S2", "rank": 2, "write": "allow", "reveal": False},
        {"name": "S3", "rank": 3, "write": "redact", "reveal": False},
    ]
    runtime = assemble_server_runtime(config=settings)
    sources = SourceLedger()
    sources.begin(ACTOR, fresh=True)
    inspector = create_text_inspector()
    reader = SdkMemoryReader(
        runtime.api,
        sources,
        inspector,
        provider="allow-sdk",
        scope_for=lambda _: SCOPE,
        security_for=lambda _: sdk_security(SCOPE),
        authorize_use=lambda *args: True,
        detector_version_for=lambda _: inspector.version,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    try:
        unit = runtime.api.add("电话 13800138000", SCOPE, security=sdk_security(SCOPE))[0]
        assert "13800138000" in unit.content
        view = reader.get(unit.id, ACTOR)
        assert "13800138000" not in view.content
        assert sources.snapshot(ACTOR).level == "S2"
    finally:
        reader.close()
        sources.close()
        runtime.close()


def test_reader_close_invalidates_prior_context_even_if_payload_is_plain(memory_runtime):
    runtime, sources, reader, _ = memory_runtime
    unit = runtime.api.add("电话 13800138000", SCOPE, security=sdk_security(SCOPE))[0]
    reader.get(unit.id, ACTOR)
    snapshot = sources.snapshot(ACTOR)
    reader.close()
    assert sources.snapshot(ACTOR).level == snapshot.level
    assert sources.snapshot(ACTOR).version != snapshot.version
    with pytest.raises(SecurityDenied):
        with sources.lease(ACTOR, snapshot.version):
            pytest.fail("已关闭的来源提供方不能继续授权")


def test_sdk_snapshot_content_tampering_with_same_revision_is_rejected(memory_runtime, monkeypatch):
    import copy

    runtime, sources, reader, _ = memory_runtime
    unit = runtime.api.add("电话 13800138000", SCOPE, security=sdk_security(SCOPE))[0]
    reader.get(unit.id, ACTOR)
    snapshot = sources.snapshot(ACTOR)
    modified = copy.deepcopy(unit)
    modified.segments[0].content += " changed"
    monkeypatch.setattr(runtime.api, "get", lambda *a, **k: modified)
    with pytest.raises(SecurityDenied):
        with sources.lease(ACTOR, snapshot.version):
            pytest.fail("同修订号的不同实际快照不能复用批准")


def test_sensitive_sdk_write_exemption_not_attested_as_safe_view(tmp_path):
    from jiuwen_memory.api import assemble_server_runtime

    settings = sdk_settings(tmp_path)
    levels = [
        {"name": "S1", "rank": 1, "write": "allow"},
        {"name": "S2", "rank": 2, "write": "allow", "reveal": False},
        {"name": "S3", "rank": 3, "write": "redact", "reveal": False},
    ]
    rules = [{"level": "S2", "pattern": "internal-project-word"}]
    settings["privacy"]["default"]["params"].update(levels=levels, rules=rules)
    runtime = assemble_server_runtime(config=settings)
    sources = SourceLedger()
    sources.begin(ACTOR, fresh=True)
    inspector = create_text_inspector()
    reader = SdkMemoryReader(
        runtime.api,
        sources,
        inspector,
        provider="custom-sdk",
        scope_for=lambda _: SCOPE,
        security_for=lambda _: sdk_security(SCOPE),
        authorize_use=lambda *a: True,
        detector_version_for=lambda _: inspector.version,
        protected_source_levels_for=lambda _: frozenset(),
    )
    try:
        unit = runtime.api.add("internal-project-word", SCOPE, security=sdk_security(SCOPE))[0]
        assert unit.system_metadata["privacy_level"] == "S2"
        assert "internal-project-word" in unit.content
        with pytest.raises(SecurityDenied):
            reader.get(unit.id, ACTOR)
        assert sources.snapshot(ACTOR).references == ()
    finally:
        reader.close()
        sources.close()
        runtime.close()


def test_sdk_cannot_substitute_another_id_before_source_registration(memory_runtime, monkeypatch):
    import copy

    runtime, sources, reader, _ = memory_runtime
    unit = runtime.api.add("电话 13800138000", SCOPE, security=sdk_security(SCOPE))[0]
    changed = copy.deepcopy(unit)
    changed.id = "different-memory"
    monkeypatch.setattr(runtime.api, "get", lambda *a, **k: changed)
    with pytest.raises(SecurityDenied):
        reader.get(unit.id, ACTOR)
    assert sources.snapshot(ACTOR).references == ()


@pytest.mark.parametrize("plugin_stronger", [False, True])
def test_plugin_and_sdk_rules_can_differ_without_lowering_source(tmp_path, plugin_stronger):
    from jiuwen_memory.api import assemble_server_runtime

    settings = sdk_settings(tmp_path)
    rule = [{"level": "S3" if plugin_stronger else "S2", "pattern": "internal-project-word"}]
    if not plugin_stronger:
        settings["privacy"]["default"]["params"]["rules"] = rule
    inspector = (
        create_text_inspector(rules=rule, use_builtin_rules=True)
        if plugin_stronger
        else create_text_inspector()
    )
    runtime = assemble_server_runtime(config=settings)
    sources = SourceLedger()
    sources.begin(ACTOR, fresh=True)
    reader = SdkMemoryReader(
        runtime.api,
        sources,
        inspector,
        provider="different-rules",
        scope_for=lambda _: SCOPE,
        security_for=lambda _: sdk_security(SCOPE),
        authorize_use=lambda *args: True,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    try:
        unit = runtime.api.add("internal-project-word", SCOPE, security=sdk_security(SCOPE))[0]
        assert unit.system_metadata["privacy_level"] == ("S1" if plugin_stronger else "S2")
        view = reader.get(unit.id, ACTOR)
        assert "internal-project-word" not in view.content and "[PRIVATE]" in view.content
        assert sources.snapshot(ACTOR).level == ("S3" if plugin_stronger else "S2")
        assert sources.verify(ACTOR, sources.snapshot(ACTOR).version)
    finally:
        reader.close()
        sources.close()
        runtime.close()
