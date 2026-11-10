"""Actual LLM wrappers, frozen configuration and encrypted persistence; only HTTP SDK is fake."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import assemble_managed_runtime
from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.type_def import ChatMessage
from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("target", ["openai", "dashscope"])
def test_late_nonshared_models_use_entire_applied_snapshot(
    tmp_path, clients, managed_models, target,
):
    runtime = assemble_managed_runtime(
        {"llm": {"default": {
            "target": target, "new_instance": True,
            "params": {
                "llm_model": "old", "llm_api_key": "old-synthetic-key", "llm_max_tokens": 23,
            },
        }}},
        store=SQLiteOverlayStore(tmp_path / "config.db"),
        llm_instances=["default"], authorize=lambda sec, op: True,
    )
    try:
        resolve = LlmProducer.bind_named("default", managed_models[0].source.context)
        assert runtime.configuration.save(0, {
            "llm.default.params.llm_model": "new",
            "llm.default.params.llm_api_key": "new-synthetic-key",
            "llm.default.params.llm_max_tokens": 71,
            "llm.default.params.llm_temperature": 0.7,
            "llm.default.params.llm_timeout": 11,
            "llm.default.params.llm_max_retries": 2,
        }, security=object())["applied"]
        first, second = resolve(), resolve()
        assert first is not second, "new_instance must still produce distinct handles"
        for handle in (first, second):
            assert handle.chat([ChatMessage(role="user", content="hello")]) == "new"
            client = clients[0][-1]
            assert client.settings["api_key"] == "new-synthetic-key"
            assert client.settings["timeout"].read == 11
            assert client.settings["max_retries"] == 2
            assert client.last_request["max_tokens"] == 71
            assert client.last_request["temperature"] == 0.7
        assert runtime.configuration.save(
            1, {"llm.default.target": "echo"}, security=object()
        )["applied"]
        assert resolve().chat([ChatMessage(role="user", content="echo text")]) == "echo text"
    finally:
        runtime.close()


def runtime_at(path):
    return assemble_managed_runtime(
        {
            "llm": {
                "default": {
                    "target": "openai",
                    "params": {
                        "llm_model": "old",
                        "llm_base_url": "https://old.invalid/v1",
                        "llm_api_key": "old-synthetic-key",
                        "llm_max_tokens": 23,
                    },
                }
            }
        },
        store=SQLiteOverlayStore(path),
        llm_instances=["default"],
        authorize=lambda sec, op: True,
    )


@pytest.fixture
def clients(monkeypatch):
    import openai

    created = []
    entered, release = Event(), Event()

    class Client:
        def __init__(self, **kwargs):
            self.settings = kwargs
            self.closed = False
            self.last_request = {}
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            created.append(self)

        def create(self, **kwargs):
            assert not self.closed
            if kwargs["model"] == "blocked":
                entered.set()
                assert release.wait(5)
                assert not self.closed
            self.last_request = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=kwargs["model"]))]
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(openai, "OpenAI", Client)
    return created, entered, release


def test_apply_complete_group_credential_persistence_and_restore(tmp_path, clients, managed_models):
    path = tmp_path / "config.db"
    runtime = runtime_at(path)
    try:
        llm = managed_models[-1]
        assert llm.chat([ChatMessage(role="user", content="hello")]) == "old"
        result = runtime.configuration.save(
            0,
            {
                "llm.default.params.llm_model": "new",
                "llm.default.params.llm_base_url": "https://new.invalid/v1",
                "llm.default.params.llm_api_key": "new-synthetic-key",
                "llm.default.params.llm_max_tokens": 71,
                "llm.default.params.llm_timeout": 11,
            },
            security=object(),
        )
        assert result["applied"]
        assert managed_models[-1] is llm
        assert llm.chat([ChatMessage(role="user", content="hello")]) == "new"
        client = clients[0][-1]
        assert client.settings["api_key"] == "new-synthetic-key"
        assert client.settings["base_url"] == "https://new.invalid/v1"
        assert client.last_request["max_tokens"] == 71
        assert clients[0][0].closed
        assert "new-synthetic-key" not in repr(runtime.configuration.read(security=object()))
        assert b"new-synthetic-key" not in path.read_bytes()
        old_values = SQLiteOverlayStore(path).read().values
        with pytest.raises(ConflictError):
            runtime.configuration.save(
                0, {"llm.default.params.llm_api_key": "uncommitted"}, security=object()
            )
        assert SQLiteOverlayStore(path).read().values == old_values
    finally:
        runtime.close()
    assert clients[0][-1].closed
    reopened = runtime_at(path)
    try:
        llm = managed_models[-1]
        assert llm.chat([ChatMessage(role="user", content="again")]) == "new"
        assert clients[0][-1].settings["api_key"] == "new-synthetic-key"
        assert reopened.configuration.save(
            1, {}, restore=["llm.default.params.llm_model"], security=object()
        )["applied"]
        assert llm.chat([ChatMessage(role="user", content="again")]) == "old"
    finally:
        reopened.close()


def test_inflight_request_retains_old_client_until_finished(tmp_path, clients, managed_models):
    runtime = runtime_at(tmp_path / "config.db")
    try:
        llm = managed_models[-1]
        runtime.configuration.save(
            0, {"llm.default.params.llm_model": "blocked"}, security=object()
        )
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(llm.chat, [ChatMessage(role="user", content="hello")])
            assert clients[1].wait(5)
            old = clients[0][-1]
            runtime.configuration.save(
                1, {"llm.default.params.llm_model": "new"}, security=object()
            )
            assert not old.closed
            assert llm.chat([ChatMessage(role="user", content="hello")]) == "new"
            clients[2].set()
            assert future.result(5) == "blocked"
            assert old.closed
    finally:
        clients[2].set()
        runtime.close()


def test_shared_handle_switches_provider_and_restores_startup_model(
    tmp_path, clients, managed_models,
):
    runtime = runtime_at(tmp_path / "config.db")
    try:
        handle = managed_models[-1]
        assert runtime.configuration.save(0, {"llm.default.target": "echo"}, security=object())[
            "applied"
        ]
        assert handle.chat([ChatMessage(role="user", content="echo text")]) == "echo text"
        endpoint = {
            "llm.default.params.llm_base_url": "https://dashscope.invalid/v1",
            "llm.default.params.llm_api_key": "dashscope-key",
            "llm.default.params.llm_model": "qwen-plus",
        }
        assert runtime.configuration.save(
            1,
            {**endpoint, "llm.default.target": "dashscope",
             "llm.default.params.enable_thinking": True},
            security=object(),
        )["applied"]
        assert runtime.configuration.save(
            2, {}, restore=["llm.default.target", *endpoint], security=object()
        )["applied"]
        assert handle.chat([ChatMessage(role="user", content="hello")]) == "old"
    finally:
        runtime.close()
