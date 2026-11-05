"""已配置后端在装配期加载依赖，避免服务首写在线程池中冷加载 DLL。"""

import asyncio
import logging
import subprocess
import sys
import threading
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import assemble_server_runtime
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage import _support
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.entity_store import EntityStoreProducer
from jiuwen_memory.storage.fulltext import FulltextProducer
from jiuwen_memory.storage.fusion import FusionProducer
from jiuwen_memory.storage.types import Document
from jiuwen_memory.storage.vector import VectorProducer

pytestmark = pytest.mark.unit

_BACKENDS = [
    (FulltextProducer, "elasticsearch", {"hosts": "http://es:9200"}, "elasticsearch"),
    (EntityStoreProducer, "elasticsearch", {"hosts": "http://es:9200"}, "elasticsearch"),
    (VectorProducer, "milvus", {"uri": "http://milvus:19530", "dim": 8}, "pymilvus"),
    (FusionProducer, "milvus_graph", {"uri": "http://milvus:19530", "dim": 8}, "pymilvus"),
]


@pytest.fixture
def backend_params(producer, params, tmp_path):
    if producer is FusionProducer:
        return {**params, "working_dir": str(tmp_path)}
    return params


@pytest.fixture
def backend_imports(monkeypatch):
    """用可观察导入和客户端桩验证时序，不依赖已安装的三方包或服务。"""
    register_backends()
    events = []
    client = Mock()
    client.indices.exists.return_value = False
    client.bulk.return_value = {"errors": False}
    constructor = Mock(return_value=client)
    load = _support.import_required
    for name, attribute in (("elasticsearch", "Elasticsearch"), ("pymilvus", "MilvusClient")):
        module = ModuleType(name)
        setattr(module, attribute, constructor)
        monkeypatch.setitem(sys.modules, name, module)

    def observe(name):
        events.append((name, threading.current_thread()))
        return load(name)

    monkeypatch.setattr(_support, "import_required", observe)
    return events, constructor, client


@pytest.mark.parametrize("producer,target,params,module_name", _BACKENDS)
def test_backend_build_preloads_dependency_without_connecting(
    backend_imports, producer, target, backend_params, module_name,
):
    events, constructor, _ = backend_imports
    producer.build(target, backend_params, AssemblyContext())

    assert events == [(module_name, threading.current_thread())]
    constructor.assert_not_called()


@pytest.mark.parametrize("producer,target,params,module_name", _BACKENDS)
@pytest.mark.parametrize("error_type", [ImportError, OSError, RuntimeError, ValueError])
def test_backend_dependency_failure_is_reported_at_build(
    monkeypatch, producer, target, backend_params, module_name, error_type,
):
    register_backends()
    error = error_type("dependency unavailable")

    def fail(name):
        assert name == module_name
        raise error

    monkeypatch.setattr(_support, "import_required", fail)
    with pytest.raises(BackendError, match=f"{module_name} dependency failed to load") as caught:
        producer.build(target, backend_params, AssemblyContext())
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, asyncio.CancelledError])
def test_preload_does_not_wrap_process_control_or_cancellation(monkeypatch, error_type):
    error = error_type("preload interrupted")

    def fail(_name):
        raise error

    monkeypatch.setattr(_support, "import_required", fail)
    with pytest.raises(error_type) as caught:
        _support.preload_backend("elasticsearch")
    assert caught.value is error


def test_preload_preserves_process_exit_in_subprocess():
    # G.ERR.11：退出信号只在独立进程主入口构造、捕获，不让测试进程承担退出风险。
    script = dedent("""
        import sys
        from unittest.mock import patch
        from jiuwen_memory.storage import _support

        signal = SystemExit("preload interrupted")
        with patch.object(_support, "import_required", side_effect=signal):
            try:
                _support.preload_backend("elasticsearch")
            except SystemExit as caught:
                assert caught is signal, "process exit must propagate unchanged"
            else:
                raise AssertionError("process exit was swallowed")
        sys.stdout.write("process-exit-preserved")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "process-exit-preserved", result.stdout


def test_entity_without_hosts_does_not_load_dependency(backend_imports):
    events, constructor, _ = backend_imports
    assert EntityStoreProducer.build("elasticsearch", {}, AssemblyContext()) is None
    assert events == []
    constructor.assert_not_called()


@pytest.mark.parametrize("error_type", [ImportError, OSError, RuntimeError, ValueError])
def test_fusion_dependency_failure_does_not_create_working_directory(
    monkeypatch, tmp_path, error_type,
):
    register_backends()
    working_dir = tmp_path / "not-created" / "fusion"

    def fail(name):
        assert name == "pymilvus"
        raise error_type("dependency unavailable")

    monkeypatch.setattr(_support, "import_required", fail)
    with pytest.raises(BackendError, match="pymilvus dependency failed to load"):
        FusionProducer.build("milvus_graph", {
            "uri": "http://milvus:19530", "dim": 8, "working_dir": str(working_dir),
        }, AssemblyContext())
    assert not working_dir.parent.exists(), "dependency failure must not create directories"


@pytest.mark.parametrize("producer", [VectorProducer, FusionProducer])
@pytest.mark.parametrize("dim", [0, -1])
def test_invalid_milvus_dimension_is_reported_before_dependency_loading(
    backend_imports, producer, tmp_path, dim,
):
    events, constructor, _ = backend_imports
    working_dir = tmp_path / "not-created" / "fusion"
    params = {"uri": "http://milvus:19530", "dim": dim, "working_dir": str(working_dir)}
    target = "milvus" if producer is VectorProducer else "milvus_graph"
    backend = "milvus vector store" if producer is VectorProducer else "milvus_graph fusion"
    with pytest.raises(ValidationError, match=f"{backend} requires positive 'dim'"):
        producer.build(target, params, AssemblyContext())
    assert events == []
    constructor.assert_not_called()
    assert not working_dir.parent.exists(), "invalid dimensions must not create directories"


@pytest.mark.parametrize("producer,target,params,module_name", _BACKENDS)
def test_server_assembly_preloads_before_background_start(
    backend_imports, producer, target, backend_params, module_name,
):
    events, constructor, _ = backend_imports
    runtime = assemble_server_runtime(config={
        "security": {"default": {"target": "standard", "params": {
            "authenticator": {"target": "dev"},
        }}},
        producer.TOP_NAME: {"default": {"target": target, "params": backend_params}},
    })
    try:
        assert events == [(module_name, threading.current_thread())]
        asyncio.run(runtime.start_background())
        assert events == [(module_name, threading.current_thread())]
        constructor.assert_not_called()
    finally:
        runtime.close()


def test_offline_server_does_not_load_backend_dependencies(backend_imports):
    events, constructor, _ = backend_imports
    runtime = assemble_server_runtime(security_runtime=None)
    try:
        asyncio.run(runtime.start_background())
        assert events == []
        constructor.assert_not_called()
    finally:
        runtime.close()


def test_first_write_keeps_connection_and_index_creation_lazy(backend_imports):
    events, constructor, client = backend_imports
    store = FulltextProducer.build("elasticsearch", {"hosts": "http://es:9200"}, AssemblyContext())
    constructor.assert_not_called()

    async def write():
        await asyncio.to_thread(store.insert, Scope(org="test"), [Document(id="1", text="hello")])

    asyncio.run(write())
    assert events == [("elasticsearch", threading.current_thread())]
    constructor.assert_called_once()
    client.indices.create.assert_called_once()
    client.bulk.assert_called_once()


def test_preload_logs_start_and_completion_with_elapsed_time(backend_imports, monkeypatch, caplog):
    monkeypatch.setattr(_support, "logger", logging.getLogger("backend-preload-test"))
    with caplog.at_level(logging.INFO, logger="backend-preload-test"):
        _support.preload_backend("elasticsearch")
    assert "import started: elasticsearch" in caplog.text
    assert "import completed: elasticsearch elapsed=" in caplog.text
