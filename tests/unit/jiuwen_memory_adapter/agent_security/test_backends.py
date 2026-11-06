"""后端目录可独立扩展；配置选型不强行归一化各后端的记忆操作。"""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwen_memory_adapter.agent_security import (
    Actor,
    BackendContext,
    BackendOperation,
    BackendResult,
    MemoryBackendRegistry,
    SecurityDenied,
    create_backend_registry,
)
from jiuwen_memory_adapter.agent_security.backends import verify_backend_result
from jiuwen_memory_adapter.agent_security.config import AgentSecurityConfig
from jiuwen_memory_adapter.agent_security.sources import SourceLedger, SourceReference

pytestmark = pytest.mark.unit


def test_framework_and_memory_backend_are_independent_selectors():
    config = AgentSecurityConfig.from_mapping(
        {"framework": "openjiuwen", "memory_backend": "jiuwen"}
    )
    assert config.framework == "openjiuwen" and config.memory_backend == "jiuwen"
    assert replace(config, framework="future-framework").memory_backend == "jiuwen"
    assert AgentSecurityConfig("openjiuwen").memory_backend is None


def test_backend_can_publish_an_operation_unrelated_to_crud():
    class Notes:
        backend_id = "notes"
        operations = (
            BackendOperation(
                "notes_walk_edges",
                "Walk native edges",
                '{"type":"object","properties":{"root":{"type":"string"}}}',
            ),
        )

        def __init__(self, context):
            self.sources = context.sources
            self.sources.register_provider("notes", self.validate)

        @staticmethod
        def validate(actor, ref):
            return ref.unit_id == "n1"

        def invoke(self, operation, arguments, actor):
            assert operation == "notes_walk_edges"
            reference = SourceReference(
                "notes",
                (actor.tenant_id, "", actor.user_id, "", ""),
                "n1",
                "revision-1",
                "S2",
                "walk-edges",
            )
            state = self.sources.record(actor, [reference])
            return BackendResult({"edges": [], "root": arguments["root"]}, state)

        def close(self):
            self.sources.unregister_provider("notes", self.validate)

    registry = MemoryBackendRegistry()
    sources = SourceLedger()
    actor = Actor("org", "user", "thread", "agent", "1")
    sources.begin(actor, fresh=True)
    registry.register("notes", Notes)
    backend = registry.create("notes", BackendContext(sources, None, None))
    result = backend.invoke("notes_walk_edges", {"root": "n1"}, actor)
    assert verify_backend_result(result, actor=actor, sources=sources) == {
        "edges": [],
        "root": "n1",
    }
    assert not hasattr(backend, "get") and not hasattr(backend, "search")
    with pytest.raises(SecurityDenied):
        registry.register("notes", lambda context: Notes())
    with pytest.raises(SecurityDenied):
        create_backend_registry().create("notes", BackendContext(None, None, None))
    sources.close()


def test_detector_and_plugin_import_without_any_memory_sdk():
    root = str(Path(__file__).resolve().parents[4])
    code = f"""
import importlib.abc,sys
sys.path.insert(0,{root!r})
class NoMemorySdk(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname == 'jiuwen_memory' or fullname.startswith('jiuwen_memory.'):
   raise AssertionError('Memory SDK imported: '+fullname)
sys.meta_path.insert(0,NoMemorySdk())
from jiuwen_memory_adapter.agent_security import (
 create_text_inspector,SecurityPlugin,EgressPolicy,TargetRule)
inspector=create_text_inspector()
assert inspector.inspect('api_key=private-value').level_id=='S3'
plugin=SecurityPlugin({{'framework':'openjiuwen'}},policy=EgressPolicy([TargetRule('current-user')]),
 targets=[],authorize=lambda op:True,authorize_input=lambda *args:True,
 authorize_transfer=lambda *args:True)
assert plugin.config.memory_backend is None
plugin.close()
assert not any(k=='jiuwen_memory' or k.startswith('jiuwen_memory.') for k in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_backend_cannot_silently_release_unlabelled_nonempty_results():
    sources = SourceLedger()
    actor = Actor("org", "user", "thread", "agent", "1")
    sources.begin(actor, fresh=True)
    try:
        with pytest.raises(SecurityDenied):
            verify_backend_result({"text": "unknown"}, actor=actor, sources=sources)
        with pytest.raises(SecurityDenied):
            verify_backend_result(
                BackendResult({"text": "unknown"}, sources.snapshot(actor)),
                actor=actor,
                sources=sources,
            )
        assert (
            verify_backend_result(
                BackendResult([], sources.snapshot(actor)), actor=actor, sources=sources
            )
            == []
        )
        with pytest.raises(SecurityDenied):
            BackendOperation("notes_write", "Write", '{"type":"object"}', effect="write")
    finally:
        sources.close()


def test_backend_must_publish_validated_operation_descriptors():
    closed = []
    registry = MemoryBackendRegistry()
    registry.register(
        "invalid",
        lambda context: SimpleNamespace(
            backend_id="invalid",
            operations=(SimpleNamespace(name="write", effect="write"),),
            close=lambda: closed.append(True),
        ),
    )
    with pytest.raises(SecurityDenied, match="BackendOperation"):
        registry.create("invalid", BackendContext(None, None, None))
    assert closed == [True]
