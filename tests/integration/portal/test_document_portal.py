"""Real Markdown/shadow storage, SDK plugin, local browser authentication and HTTP calls."""

import asyncio
import json
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.cookiejar import CookieJar
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest

from jiuwen_memory.api import (
    AuthenticationError,
    BackendError,
    Credentials,
    Scope,
    Surface,
    build_dev_authenticator,
    internal_context,
    new_request_context,
)
from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
    AgentMemoryMemoryProvider,
    _agent_default_config,
)

pytestmark = pytest.mark.integration
requires_shared_portal = pytest.mark.skipif(
    os.name not in ("posix", "nt"), reason="requires a supported shared SDK Portal platform",
)


@pytest.fixture
def document_watchdogs(monkeypatch):
    from watchdog import observers
    from watchdog.observers.polling import PollingObserver

    from jiuwen_memory.common.factory.factory import Factory
    from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog

    monkeypatch.setattr(observers, "Observer", PollingObserver)
    watchdogs = []

    def record(instance):
        if isinstance(instance, LocalWatchdog):
            watchdogs.append(instance)

    with Factory.observe_builds(record):
        yield watchdogs


def document_config(root, *, watch=False, registry=None):
    config = _agent_default_config()
    config["globals"].update(
        {"embedder_dim": 64, "watch_document": watch, "llm_api_key": "", "embedder_api_key": ""}
    )
    config["llm"] = {"default": "echo"}
    config["extractor"] = {"default": "keyword"}
    config["tokenizer"] = {"default": "whitespace"}
    config["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    config["fulltext_store"] = {"default": "memory"}
    config["markdown_store"] = {"default": {"target": "local", "params": {"root": str(root)}}}
    config["shadow_index"]["default"]["params"]["db_path"] = str(root / "shadow.db")
    if registry is not None:
        config["kv_store"]["registry"] = {
            "target": "sqlite", "params": {"db_path": str(registry), "fixed_path": True},
        }
        config["continuity"] = {"default": {"target": "local", "params": {
            "mode": "continuity", "single_writer": True, "registry_store": "registry",
        }}}
    return config


def personal_list(**filters):
    return {"extensions": {"personal": filters}}


def personal_search(query, *, source=None, top_k=10):
    return {
        "query": query, "top_k": top_k,
        "context": {"extensions": {"personal": {"source": source}}},
    }


def record_target(unit):
    return {"scope": unit["scope"], "unit_id": unit["id"]}


def memory_content(unit):
    return "\n".join(segment["content"] for segment in unit["segments"])


class Browser:
    def __init__(self, url, *, opener=None):
        parts = urlsplit(url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.query = parts.query
        self.token = parse_qs(parts.fragment)["access"][0]
        self.opener = opener or build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.headers = {}
        status, session = self.call("bootstrap", {"token": self.token})
        assert status == 200, session
        self.shared = session.get("shared", False)
        if "session_token" in session:
            self.headers["X-Memory-Portal-Session"] = session["session_token"]

    def call(self, action, body, *, origin=None):
        request = Request(
            self.origin + ("/portal/api/" if action == "bootstrap" else "/v1/") + action,
            json.dumps(body).encode(),
            headers={
                "Origin": origin or self.origin, "Content-Type": "application/json",
                "X-Memory-Portal-Browser": "1",
                **self.headers,
            },
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)


def _shared_portal_worker(pipe, start, root, instance_id, port, user, registry=None):
    from jiuwen_memory_entry.portal import shared

    broker_pid = None

    def launch(*args, **kwargs):
        nonlocal broker_pid
        process = subprocess.Popen(*args, **kwargs)
        broker_pid = process.pid
        return process

    async def run():
        auth = build_dev_authenticator(
            identities={"test": {"actor": {"org": "local", "user": user}}},
        )

        def security():
            return new_request_context(
                auth.authenticate(Credentials(api_key="test")), surface=Surface.SDK,
            )

        portal = {"port": port}
        if instance_id != "default":
            portal["instance_id"] = instance_id
        provider = AgentMemoryMemoryProvider(
            config_dict=document_config(root, registry=registry),
            continuity_enabled=registry is not None,
            continuity_mode="continuity",
            security_provider=security,
            portal=portal,
        )
        try:
            if not await asyncio.to_thread(start.wait, 15):
                raise TimeoutError("test did not release SDK workers")
            await provider.initialize(
                user_id=user, scope_id="local", agent_id=instance_id, session_id="",
            )
            while True:
                command = await asyncio.to_thread(pipe.recv)
                if command == "stop":
                    break
                if command == "status":
                    pipe.send(provider.portal_status)
                    continue
                assert command == "open"
                link = await provider.open_portal()
                pipe.send((link or provider.portal_status, broker_pid))
        finally:
            await provider.shutdown()

    launcher = SimpleNamespace(Popen=launch, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE)
    with pipe, patch.object(shared, "subprocess", launcher):
        asyncio.run(run())


@requires_shared_portal
def test_sdk_processes_share_portal_and_restore_separate_configuration(tmp_path):
    from jiuwen_memory_entry.portal.shared import SharedPortalLocation, _listening

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    root = tmp_path.resolve() / "Library"
    root.mkdir()
    alias = root.with_name("library")
    if not alias.exists():
        alias = root
    location = SharedPortalLocation((str(root), str(root / "shadow.db")))

    @contextmanager
    def worker(instance_id, *, user="developer", library=root):
        pipe, remote = context.Pipe()
        process = context.Process(
            target=_shared_portal_worker, args=(remote, start, library, instance_id, port, user),
        )
        process.start()
        remote.close()
        try:
            yield pipe, process
        finally:
            if process.is_alive():
                pipe.send("stop")
                process.join(timeout=10)
            pipe.close()
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            assert process.exitcode == 0

    def opened(pipe):
        assert pipe.poll(20), "SDK worker did not return a Portal link"
        link, pid = pipe.recv()
        assert isinstance(link, str), link
        browser = Browser(link, opener=opener)
        assert browser.shared is True
        return browser, pid

    def settings(browser, instance_id, model):
        status, result = browser.call("read_config", {"instance_id": instance_id})
        assert status == 200 and result["available"], result
        field = next(item for item in result["fields"] if item["key"] == model_key)
        assert field["effective"] == model, result
        assert result["applied_version"] == result["version"] == 1, result

    model_key = "llm.default.params.llm_model"
    key_field = "llm.default.params.llm_api_key"
    with worker("default") as first, worker("agent-b", library=alias) as second:
        pipe_a, process_a = first
        pipe_b, process_b = second
        pipe_a.send("open")
        pipe_b.send("open")
        start.set()
        (a, pid_a), (b, pid_b) = opened(pipe_a), opened(pipe_b)
        assert a.origin == b.origin == f"http://127.0.0.1:{port}"
        assert a.token != b.token
        assert a.query and b.query and a.query != b.query
        assert (pid_a is None) != (pid_b is None), (pid_a, pid_b)
        first_broker = pid_a or pid_b
        instances = [
            (a, "default", "model-a", pipe_a, process_a),
            (b, "agent-b", "model-b", pipe_b, process_b),
        ]
        status, catalog = a.call("read_config", {})
        assert status == 200 and catalog == {
            "instances": ["agent-b", "default"], "default_instance_id": "default",
        }
        for browser, agent, _, _, _ in instances:
            status, result = browser.call(
                "add", {"content": f"memory from {agent}", "scope": {}},
            )
            assert status == 200 and result[0]["scope"]["agent"] == agent, result
        for browser in (a, b):
            status, result = browser.call("list", personal_list())
            assert status == 200 and result["count"] == 2, result

        def save(instance_id, model):
            return a.call("save_config", {
                "instance_id": instance_id, "version": 0,
                "changes": {model_key: model, key_field: f"key-{model}"},
            })

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(save, "default", "model-a"),
                executor.submit(save, "agent-b", "model-b"),
            ]
            for future in futures:
                status, result = future.result(timeout=10)
                assert status == 200 and result["applied"], result
        assert save("default", "stale-model")[0] == 409
        assert a.call("save_config", {"version": 1, "changes": {model_key: "mistake"}})[0] == 400
        settings(a, "default", "model-a")
        settings(a, "agent-b", "model-b")
        assert a.call("save_config", {"instance_id": "agent-b", "retry": True}) == (
            200, {"version": 1, "applied": True},
        )
        status, page = a.call("list", personal_list())
        assert status == 200 and page["add_context"]["agent"] == "default", page

        with worker("other-user", user="other") as (other_pipe, _):
            other_pipe.send("open")
            other, _ = opened(other_pipe)
            assert a.call("read_config", {})[1]["instances"] == ["agent-b", "default"]
            assert other.call("read_config", {})[1]["instances"] == ["other-user"]
            assert other.call("read_config", {"instance_id": "default"})[0] == 404
            assert other.call("save_config", {"instance_id": "default", "retry": True})[0] == 404

        duplicate_auth = internal_context(build_dev_authenticator())
        duplicate = AgentMemoryMemoryProvider(
            config_dict=document_config(alias),
            security_provider=lambda: duplicate_auth,
        )
        try:
            asyncio.run(duplicate.initialize())
            assert asyncio.run(duplicate.open_portal()) is None
            assert "instance_id 'default' is already in use" in duplicate.portal_status["reason"]
            assert duplicate.runtime.configuration is None
            assert asyncio.run(duplicate.runtime.api.add_async(
                "memory while the Portal ID is occupied", Scope(org="local", user="developer"),
                security=duplicate_auth,
            ))
        finally:
            asyncio.run(duplicate.shutdown())

        owner, survivor = instances if pid_a else reversed(instances)
        old_tab, owner_id, owner_model, owner_pipe, owner_process = owner
        _, survivor_id, survivor_model, survivor_pipe, _ = survivor
        owner_pipe.send("stop")
        owner_process.join(timeout=10)
        assert owner_process.exitcode == 0
        assert old_tab.call("read_config", {"instance_id": owner_id})[0] == 404
        assert old_tab.call("read_config", {})[1]["instances"] == [survivor_id]
        assert old_tab.call("list", personal_list())[1]["count"] == 3
        settings(old_tab, survivor_id, survivor_model)

        with worker(owner_id) as (restarted, _):
            restarted.send("open")
            current, pid = opened(restarted)
            assert pid is None
            settings(current, owner_id, owner_model)
            settings(old_tab, owner_id, owner_model)
            os.kill(first_broker, getattr(signal, "SIGKILL", signal.SIGTERM))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) != 0:
                        break
                time.sleep(0.025)
            restarted.send("status")
            assert restarted.poll(5), "SDK worker did not return Portal status"
            status = restarted.recv()
            assert not status["available"] and status["address"] is None
            restarted.send("open")
            recovered, replacement = opened(restarted)
            assert replacement is not None and replacement != first_broker
            # Only one host has opened a new link. The other Runtime is already discoverable.
            assert set(recovered.call("read_config", {})[1]["instances"]) == {
                owner_id, survivor_id,
            }
            settings(recovered, survivor_id, survivor_model)
            survivor_pipe.send("open")
            survivor_recovered, other_replacement = opened(survivor_pipe)
            assert recovered.origin == survivor_recovered.origin == a.origin
            assert other_replacement is None
            assert current.call("read_config", {})[0] == 401
            settings(recovered, owner_id, owner_model)
            settings(survivor_recovered, survivor_id, survivor_model)
    deadline = time.monotonic() + 2
    while _listening(location.endpoint) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _listening(location.endpoint), "shared Portal did not close after the last worker"
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0, "Portal still listens after shutdown"
    with worker(owner_id) as (new_worker, _):
        new_worker.send("open")
        reopened, pid = opened(new_worker)
        assert reopened.origin == a.origin and pid is not None
        assert reopened.call("read_config", {})[1]["instances"] == [owner_id]
        settings(reopened, owner_id, owner_model)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows named pipes")
def test_windows_pipe_reserves_its_name_and_transfers_large_messages(tmp_path):
    from jiuwen_memory_entry.portal import _windows_ipc

    directory = _windows_ipc.private_directory(tmp_path / "portal")
    path = directory / "portal.sock"
    options = {"threaded": True, "interval": 0.05, "request_timeout": 2, "limit": 256 * 1024}
    server = _windows_ipc.server(path, **options)

    def echo(payload):
        return payload

    server.dispatch = echo
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(OSError):
            _windows_ipc.server(path, **options)
        payload = bytes(range(256)) * 300
        assert _windows_ipc.exchange(path, payload, 5, options["limit"]) == payload
    finally:
        server.shutdown()
        thread.join(5)
        assert not thread.is_alive()
        server.server_close()
    assert not _windows_ipc.listening(path, 0.05)


@requires_shared_portal
def test_document_continuity_uses_one_portal_and_one_config_per_runtime(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    pipe, remote = context.Pipe()
    process = context.Process(
        target=_shared_portal_worker,
        args=(remote, start, tmp_path, "remote", 0, "developer", tmp_path / "remote-registry.db"),
    )
    auth = build_dev_authenticator(
        identities={"test": {"actor": {"org": "local", "user": "developer"}}},
    )

    def security():
        return new_request_context(
            auth.authenticate(Credentials(api_key="test")), surface=Surface.SDK,
        )

    async def run():
        config = document_config(tmp_path, registry=tmp_path / "local-registry.db")
        first, second = [AgentMemoryMemoryProvider(
            config_dict=config, security_provider=security,
            continuity_enabled=True, continuity_mode="continuity",
            portal={"language": language},
        ) for language in ("en", "zh")]
        ordinary = AgentMemoryMemoryProvider(
            config_dict=document_config(tmp_path), security_provider=security,
            portal={"instance_id": "ordinary", "language": ""},
        )
        try:
            await first.initialize(scope_id="local")
            await second.initialize(scope_id="local")
            await ordinary.initialize(scope_id="local")
            assert first.runtime is second.runtime
            assert ordinary.runtime is not first.runtime
            first_link = await asyncio.to_thread(lambda: first.portal_url)
            launch_links = [first_link, await second.open_portal(), await ordinary.open_portal()]
            languages = [
                parse_qs(urlsplit(launch_link).fragment).get("language")
                for launch_link in launch_links
            ]
            assert languages == [["en"], ["zh-CN"], None]
            a, b, c = [
                await asyncio.to_thread(Browser, launch_link) for launch_link in launch_links
            ]
            start.set()
            pipe.send("open")
            assert await asyncio.to_thread(pipe.poll, 20), "remote Portal link missing"
            link, broker_pid = pipe.recv()
            assert isinstance(link, str), link
            assert "language" not in parse_qs(urlsplit(link).fragment)
            d = await asyncio.to_thread(Browser, link)
            assert broker_pid is None
            assert a.origin == b.origin == c.origin == d.origin
            assert all(browser.shared for browser in (a, b, c, d))
            assert a.call("read_config", {}) == (200, {
                "instances": ["default", "ordinary", "remote"], "default_instance_id": "default",
            })
            assert d.call("add", {"content": "shared task note", "scope": {}})[0] == 200
            assert c.call("list", personal_list())[1]["count"] == 1

            model_key = "llm.default.params.llm_model"
            for instance_id in ("default", "remote"):
                assert a.call("save_config", {
                    "instance_id": instance_id, "version": 0, "changes": {model_key: instance_id},
                }) == (200, {"version": 1, "applied": True})
            await first.shutdown()
            assert a.call("list", personal_list())[0] == 401
            assert a.call("read_config", {"instance_id": "remote"})[0] == 401
            assert b.call("list", personal_list())[1]["count"] == 1
            for instance_id in ("default", "remote"):
                status, settings = b.call("read_config", {"instance_id": instance_id})
                assert status == 200 and settings["available"], settings
                assert next(item["effective"] for item in settings["fields"]
                            if item["key"] == model_key) == instance_id
            await second.shutdown()
            assert d.call("read_config", {})[1]["instances"] == ["ordinary", "remote"]
            await first.initialize(scope_id="local")
            reopened = await asyncio.to_thread(Browser, await first.open_portal())
            status, settings = reopened.call("read_config", {"instance_id": "default"})
            assert status == 200 and settings["applied_version"] == 1, settings
        finally:
            await asyncio.gather(first.shutdown(), second.shutdown(), ordinary.shutdown())

    process.start()
    remote.close()
    try:
        asyncio.run(run())
    finally:
        start.set()
        if process.is_alive():
            pipe.send("stop")
            process.join(timeout=10)
        pipe.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    assert process.exitcode == 0


@requires_shared_portal
def test_continuity_portal_joins_running_runtime_and_keeps_its_principal(tmp_path):
    config = document_config(tmp_path, registry=tmp_path / "registry.db")
    auth = build_dev_authenticator(identities={
        "owner": {"actor": {"org": "local", "user": "developer"}},
        "other": {"actor": {"org": "local", "user": "other"}},
    })
    credential = "owner"

    def security():
        return new_request_context(
            auth.authenticate(Credentials(api_key=credential)), surface=Surface.SDK,
        )

    async def run():
        nonlocal credential
        options = dict(
            config_dict=config, security_provider=security,
            continuity_enabled=True, continuity_mode="continuity",
        )
        first = AgentMemoryMemoryProvider(**options, portal={"enabled": False})
        second = AgentMemoryMemoryProvider(**options)
        try:
            await first.initialize()
            await second.initialize()
            assert first.runtime is second.runtime
            assert not (tmp_path / "memory/.shadow/portal-config.db").exists()
            browser = await asyncio.to_thread(Browser, await second.open_portal())
            assert browser.shared
            status, settings = browser.call("read_config", {"instance_id": "default"})
            assert status == 200 and settings["available"] is False
            assert browser.call("list", personal_list())[0] == 200

            credential = "other"
            with pytest.raises(AuthenticationError, match="authentication principal changed"):
                await second.open_portal()
            assert browser.call("list", personal_list())[0] == 401
            credential = "owner"
            assert await second.open_portal()
        finally:
            await asyncio.gather(first.shutdown(), second.shutdown())

    asyncio.run(run())


@requires_shared_portal
@pytest.mark.parametrize("probe_before_reopen", [False, True])
def test_shared_session_skips_stale_backup_and_allows_new_credential(tmp_path, probe_before_reopen):
    auth = build_dev_authenticator().authenticate(Credentials())
    current = auth

    def security():
        return new_request_context(auth, surface=Surface.SDK)

    def backup_security():
        return new_request_context(current, surface=Surface.SDK)

    async def run():
        nonlocal current
        sources = (("opening", security), ("backup", backup_security), ("survivor", security))
        opening, backup, survivor = [
            AgentMemoryMemoryProvider(
                config_dict=document_config(tmp_path), security_provider=provider,
                portal={"instance_id": name},
            )
            for name, provider in sources
        ]
        try:
            for provider in (opening, backup, survivor):
                await provider.initialize()
            old = await asyncio.to_thread(Browser, await opening.open_portal())
            backup_tab = await asyncio.to_thread(Browser, await backup.open_portal())
            unused_link = urlsplit(await backup.open_portal())
            await opening.shutdown()

            # An error from an operation that already entered MemoryAPI must not be replayed.
            with patch.object(backup.runtime.api, "list", side_effect=AuthenticationError):
                status, _ = await asyncio.to_thread(old.call, "list", personal_list())
                assert status == 401

            current = replace(auth, credential_id="rotated")
            if probe_before_reopen:
                assert old.call("read_config", {}) == (200, {
                    "instances": ["survivor"], "default_instance_id": "survivor",
                })
                assert old.call("list", personal_list())[0] == 200
                assert backup_tab.call("list", personal_list())[0] == 401
            renewed = await asyncio.to_thread(Browser, await backup.open_portal())
            status, catalog = renewed.call("read_config", {})
            assert status == 200 and catalog["instances"] == ["backup"]
            await backup.shutdown()
            assert backup_tab.call("read_config", {})[0] == 401
            assert backup_tab.call("list", personal_list())[0] == 401
            token = parse_qs(unused_link.fragment)["access"][0]
            assert old.call("bootstrap", {"token": token})[0] == 401
            assert old.call("list", personal_list())[0] == 200
        finally:
            for provider in (opening, backup, survivor):
                await provider.shutdown()

    asyncio.run(run())


@requires_shared_portal
@pytest.mark.parametrize("close_fails", [False, True])
def test_shared_instance_id_stays_claimed_during_shutdown(tmp_path, close_fails):
    auth = build_dev_authenticator()

    async def run():
        old, replacement = [
            AgentMemoryMemoryProvider(
                config_dict=document_config(tmp_path),
                security_provider=lambda: internal_context(auth),
            )
            for _ in range(2)
        ]
        await old.initialize()
        close_runtime = old.runtime.close
        closing, finish = Event(), Event()

        def wait_to_close():
            closing.set()
            assert finish.wait(10)
            if close_fails:
                raise RuntimeError("watchdog did not stop")
            close_runtime()

        try:
            with patch.object(old.runtime, "close", wait_to_close):
                shutdown = asyncio.create_task(old.shutdown())
                try:
                    assert await asyncio.to_thread(closing.wait, 5)
                    for _ in range(2):
                        shutdown.cancel()
                        await asyncio.sleep(0)
                    await replacement.initialize()
                    assert await replacement.open_portal() is None
                    assert "instance_id 'default'" in replacement.portal_status["reason"]
                    await replacement.shutdown()
                finally:
                    finish.set()
                    expected = RuntimeError if close_fails else asyncio.CancelledError
                    with pytest.raises(expected):
                        await shutdown
                if close_fails:
                    await replacement.initialize()
                    assert await replacement.open_portal() is None
                    await replacement.shutdown()
            await old.shutdown()
            await replacement.initialize()
            assert await replacement.open_portal()
        finally:
            await old.shutdown()
            await replacement.shutdown()

    asyncio.run(run())


@requires_shared_portal
def test_shutdown_waits_for_cancelled_document_runtime_assembly(tmp_path):
    from jiuwen_memory_adapter.jiuwenswarm import agent_memory_provider as adapter

    auth = build_dev_authenticator()
    config = document_config(tmp_path, registry=tmp_path / "registry.db")
    provider = AgentMemoryMemoryProvider(
        config_dict=config, security_provider=lambda: internal_context(auth),
        continuity_enabled=True, continuity_mode="continuity",
    )
    entered, proceed = Event(), Event()
    assemble = adapter.assemble_managed_runtime

    def slow_assembly(*args, **kwargs):
        entered.set()
        assert proceed.wait(10)
        return assemble(*args, **kwargs)

    async def run():
        try:
            with patch.object(adapter, "assemble_managed_runtime", slow_assembly):
                initialize = asyncio.create_task(provider.initialize())
                assert await asyncio.to_thread(entered.wait, 5)
                initialize.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await initialize
                shutdown = asyncio.create_task(provider.shutdown())
                try:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(shutdown), timeout=0.05)
                finally:
                    proceed.set()
                    await shutdown
            await provider.initialize()
            assert await provider.open_portal()
        finally:
            proceed.set()
            await provider.shutdown()

    asyncio.run(run())


@requires_shared_portal
def test_repeated_cancellation_waits_for_continuity_portal_startup(tmp_path):
    from jiuwen_memory_entry.portal.shared import SharedPortalLocation

    auth = build_dev_authenticator()
    provider = AgentMemoryMemoryProvider(
        config_dict=document_config(tmp_path, registry=tmp_path / "registry.db"),
        security_provider=lambda: internal_context(auth),
        continuity_enabled=True, continuity_mode="continuity",
    )
    entered, proceed = Event(), Event()
    register = SharedPortalLocation.register

    def slow_register(location, registration, info):
        entered.set()
        assert proceed.wait(10)
        return register(location, registration, info)

    async def run():
        try:
            with patch.object(SharedPortalLocation, "register", slow_register):
                initialize = asyncio.create_task(provider.initialize())
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    for _ in range(2):
                        initialize.cancel()
                        await asyncio.sleep(0)
                    assert not initialize.done()
                finally:
                    proceed.set()
                    with pytest.raises(asyncio.CancelledError):
                        await initialize
            await provider.shutdown()
            await provider.initialize()
            assert await provider.open_portal()
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_sdk_document_roundtrip_correct_forget_restart_and_identity(tmp_path):
    auth = build_dev_authenticator()

    def security():
        return internal_context(auth)

    config = document_config(tmp_path)
    document_root = tmp_path / "documents"
    config["config_source"] = {"default": {
        "target": "yaml_defaults",
        "params": {"values": {"markdown_store.root": str(document_root)}},
    }}

    async def run():
        provider = AgentMemoryMemoryProvider(config_dict=config, security_provider=security)
        await asyncio.gather(provider.initialize(), provider.initialize())
        runtime = provider.runtime
        assert provider.portal_status["available"], provider.portal_status
        first_url = provider.portal_url
        port = urlsplit(first_url).port
        browser = await asyncio.to_thread(Browser, first_url)
        settings_target = {"instance_id": "default"} if browser.shared else {}

        def call(name, body):
            return asyncio.to_thread(browser.call, name, body)

        try:
            assert (await call("bootstrap", {"token": browser.token}))[0] == 401
            assert (
                await asyncio.to_thread(browser.call, "list", personal_list(), origin="http://host.example")
            )[0] == 200
            first_scope = Scope(org="local", user="developer", agent="design", session="one")
            second_scope = Scope(org="local", user="developer", agent="coding", session="two")
            await runtime.api.add_async(
                "ocean memory alpha",
                first_scope,
                security=security(),
                system_metadata={"source_app": "app-a", "project": "alpha"},
            )
            await runtime.api.add_async(
                "ocean memory beta",
                second_scope,
                security=security(),
                system_metadata={"source_app": "app-b"},
            )
            status, page = await call("list", personal_list())
            assert status == 200 and page["count"] == 2, page
            assert set(page["sources"]) == {"app-a", "app-b"}
            assert set(page["projects"]) == {"", "alpha"}, page
            assert {agent for agent, _ in page["agents"]} == {"design", "coding"}, page
            first = next(
                item for item in page["items"] if item["system_metadata"]["source_app"] == "app-a"
            )
            assert first["system_metadata"]["memory_class"] in page["memory_classes"], page
            status, combined = await call("list", personal_list(
                source="app-a", agent="design", project="alpha", query="ALPHA",
                tier=first["tier"], memory_class=first["system_metadata"]["memory_class"],
            ))
            assert status == 200 and combined["count"] == 1, combined
            assert combined["items"][0]["system_metadata"]["project"] == "alpha", combined
            status, project_hits = await call("search", personal_search("ocean", source="app-a"))
            assert status == 200 and project_hits["items"], project_hits
            assert all(
                hit["item"]["system_metadata"]["source_app"] == "app-a"
                for hit in project_hits["items"]
            ), project_hits
            status, filtered = await call("list", personal_list(source="app-a"))
            assert status == 200 and filtered["count"] == 1, filtered
            status, corrected = await call(
                "update", {**record_target(filtered["items"][0]),
                           "patch": {"content": "ocean corrected alpha"}},
            )
            assert status == 200 and corrected["supersedes"], corrected
            status, current = await call("get", record_target(corrected))
            assert status == 200 and memory_content(current) == "ocean corrected alpha", current
            status, old = await call("list", personal_list(lifecycle="superseded"))
            assert status == 200 and old["count"] == 1, old
            status, results = await call("search", personal_search("ocean", source="app-b"))
            assert status == 200 and results["items"], results
            assert all(
                hit["item"]["system_metadata"]["source_app"] == "app-b"
                for hit in results["items"]
            )
            status, forgotten = await call("delete", {
                "selector": {"scope": corrected["scope"], "unit_ids": [corrected["id"]],
                             "mode": "forget"},
            })
            assert status == 200 and forgotten, forgotten
            status, active = await call("list", personal_list())
            assert status == 200 and active["count"] == 1, active
            status, retired = await call("list", personal_list(lifecycle="forgotten"))
            assert status == 200 and retired["count"] == 1, retired
            status, settings = await call("read_config", settings_target)
            assert status == 200 and settings["available"], settings
            assert settings["instance"]["document_root"] == str(document_root)
            status, saved = await call(
                "save_config",
                {
                    **settings_target,
                    "version": settings["version"],
                    "changes": {
                        "llm.default.params.llm_model": "saved",
                        "llm.default.params.llm_api_key": "synthetic-secret",
                    },
                },
            )
            assert status == 200 and saved["applied"], saved
            status, settings = await call("read_config", settings_target)
            assert status == 200 and settings["apply_state"] == "applied"
            assert "synthetic-secret" not in json.dumps(settings)
            await provider.initialize()
            assert provider.runtime is runtime
            assert urlsplit(provider.portal_url).port == port
        finally:
            await provider.shutdown()
        restarted = AgentMemoryMemoryProvider(config_dict=config, security_provider=security)
        await restarted.initialize()
        try:
            second = await asyncio.to_thread(Browser, restarted.portal_url)
            status, retired = await asyncio.to_thread(
                second.call, "list", personal_list(lifecycle="forgotten")
            )
            assert status == 200 and retired["count"] == 1, retired
            status, active = await asyncio.to_thread(second.call, "list", personal_list())
            assert status == 200 and active["count"] == 1, active
            status, settings = await asyncio.to_thread(second.call, "read_config", settings_target)
            assert status == 200 and settings["applied_version"] == 1, settings
        finally:
            await restarted.shutdown()

    asyncio.run(run())


def test_unlabelled_source_and_page_add_are_visible_to_sdk(tmp_path):
    auth = build_dev_authenticator()

    def security():
        return internal_context(auth)

    async def run():
        provider = AgentMemoryMemoryProvider(
            config_dict=document_config(tmp_path), security_provider=security
        )
        await provider.initialize()
        try:
            scope = Scope(org="local", user="developer")
            await provider.runtime.api.add_async(
                "labelled memory",
                scope,
                security=security(),
                system_metadata={"source_app": "app-a"},
            )
            await provider.runtime.api.add_async("unlabelled memory", scope, security=security())
            browser = await asyncio.to_thread(Browser, provider.portal_url)
            status, page = await asyncio.to_thread(browser.call, "list", personal_list())
            assert status == 200 and "" in page["sources"], page
            item = next(
                entry for entry in page["items"] if memory_content(entry) == "unlabelled memory"
            )
            assert not item["system_metadata"].get("source_app"), item
            status, added = await asyncio.to_thread(
                browser.call, "add", {"content": "portal added memory", "scope": {}}
            )
            assert status == 200 and added[0]["system_metadata"]["source_app"] == "portal", added
            listing = await asyncio.to_thread(
                provider.runtime.api.list, scope, security=security()
            )
            assert any(unit.content == "portal added memory" for unit in listing.items)
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_two_browser_sessions_keep_versions_monotonic(tmp_path):
    auth = build_dev_authenticator()

    async def run():
        provider = AgentMemoryMemoryProvider(
            config_dict=document_config(tmp_path),
            security_provider=lambda: internal_context(auth),
        )
        await provider.initialize()
        try:
            first = await asyncio.to_thread(Browser, provider.portal_url)
            second = await asyncio.to_thread(Browser, provider.portal_url)
            target = {"instance_id": "default"} if first.shared else {}
            status, settings = await asyncio.to_thread(first.call, "read_config", target)
            assert status == 200 and settings["available"], settings
            version = settings["version"]
            status, saved = await asyncio.to_thread(
                second.call,
                "save_config",
                {
                    **target,
                    "version": version,
                    "changes": {"llm.default.params.llm_model": "second"},
                },
            )
            assert status == 200 and saved["applied"], saved
            status, conflict = await asyncio.to_thread(
                first.call,
                "save_config",
                {
                    **target,
                    "version": version,
                    "changes": {"llm.default.params.llm_model": "first"},
                },
            )
            assert status == 409, conflict
            status, latest = await asyncio.to_thread(first.call, "read_config", target)
            assert status == 200 and latest["version"] == version + 1, latest
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(
                    executor.map(
                        lambda browser: browser.call(
                            "save_config",
                            {
                                **target,
                                "version": version + 1,
                                "changes": {"llm.default.params.llm_temperature": 0.5},
                            },
                        ),
                        [first, second],
                    )
                )
            assert sorted(status for status, _ in outcomes) == [200, 409], outcomes
            status, after = await asyncio.to_thread(first.call, "read_config", target)
            assert status == 200 and after["version"] == version + 2, after
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_correction_waits_for_watcher_and_preserves_history(
    tmp_path, monkeypatch, document_watchdogs,
):
    from jiuwen_memory.api import assemble_runtime
    from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
    from jiuwen_memory_entry.portal.requests import invoke_browser_api

    runtime = assemble_runtime(config=document_config(tmp_path, watch=True))
    security = internal_context(build_dev_authenticator())
    scope = Scope(org="local", user="developer")
    snapshot_read, resume = Event(), Event()
    try:
        old = runtime.api.add("old content", scope, security=security)[0]
        path = tmp_path / old.system_metadata["md_filename"]
        watchdog, = document_watchdogs
        original_list = SqliteDocumentShadowIndex.list_units_by_md

        def pause_after_snapshot(index, *args, **kwargs):
            rows = original_list(index, *args, **kwargs)
            if kwargs.get("lifecycle") == "active" and not snapshot_read.is_set():
                snapshot_read.set()
                assert resume.wait(5)
            return rows

        monkeypatch.setattr(SqliteDocumentShadowIndex, "list_units_by_md", pause_after_snapshot)
        runtime.start()
        watchdog.schedule_sync(str(path))
        assert snapshot_read.wait(5), "watchdog did not start reconciliation"
        with ThreadPoolExecutor(1) as pool:
            correction = pool.submit(
                invoke_browser_api,
                runtime.api, "update",
                {"scope": vars(scope), "unit_id": old.id, "patch": {"content": "new content"}},
                security=security,
            )
            try:
                with pytest.raises(TimeoutError):
                    correction.result(timeout=0.1)
            finally:
                resume.set()
            corrected = correction.result(timeout=5)

        # Reconcile again after correction: old content must stay historical.
        # Direct reconciliation makes the post-correction result deterministic.
        # pylint: disable=protected-access
        watchdog._run_sync(str(path.relative_to(tmp_path)), False, watchdog._generation)
        # pylint: enable=protected-access
        items = runtime.api.list(scope, security=security).items
        assert sorted((item.content, item.lifecycle.value) for item in items) == [
            (memory_content(corrected), "active"), ("old content", "superseded"),
        ]
    finally:
        resume.set()
        runtime.close()


def test_watcher_sync_does_not_block_writes_in_another_document_root(
    tmp_path, monkeypatch, document_watchdogs,
):
    from jiuwen_memory.api import assemble_runtime

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = assemble_runtime(config=document_config(first_root, watch=True))
    second = assemble_runtime(config=document_config(second_root))
    security = internal_context(build_dev_authenticator())
    scope = Scope(org="local", user="developer")
    syncing, release = Event(), Event()
    try:
        original = first.api.add("first content", scope, security=security)[0]
        path = first_root / original.system_metadata["md_filename"]
        watchdog, = document_watchdogs
        # Hook the worker body to hold the actual resource locks during the test.
        sync_one = watchdog._sync_one  # pylint: disable=protected-access

        def hold_sync(md_filename, deleted):
            syncing.set()
            assert release.wait(10)
            return sync_one(md_filename, deleted)

        monkeypatch.setattr(watchdog, "_sync_one", hold_sync)
        first.start()
        watchdog.schedule_sync(str(path))
        with ThreadPoolExecutor(1) as pool:
            try:
                assert syncing.wait(5)
                written = pool.submit(second.api.add, "second content", scope, security=security)
                assert written.result(timeout=5)
            finally:
                release.set()
    finally:
        release.set()
        second.close()
        first.close()


def test_watcher_does_not_resurrect_corrected_or_forgotten_memory(tmp_path, document_watchdogs):
    auth = build_dev_authenticator()

    def security():
        return internal_context(auth)

    async def run():
        provider = AgentMemoryMemoryProvider(
            config_dict=document_config(tmp_path, watch=True), security_provider=security
        )
        await provider.initialize()
        try:
            watchdog, = document_watchdogs
            for _ in range(50):
                if watchdog.watcher_initialized:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("watchdog did not become ready")

            api = provider.runtime.api
            scope = Scope(org="local", user="developer")
            initial, = await api.add_async("initial preference", scope, security=security())
            forgotten, = await api.add_async("forgotten note", scope, security=security())
            browser = await asyncio.to_thread(Browser, provider.portal_url)
            status, corrected = await asyncio.to_thread(browser.call, "update", {
                "scope": vars(scope), "unit_id": initial.id,
                "patch": {"content": "corrected preference"},
            })
            assert status == 200, corrected
            status, result = await asyncio.to_thread(browser.call, "delete", {
                "selector": {"scope": vars(scope), "unit_ids": [forgotten.id], "mode": "forget"},
            })
            assert status == 200, result

            path = tmp_path / corrected["system_metadata"]["md_filename"]
            contents = path.read_text(encoding="utf-8")
            assert "forgotten note" not in contents
            assert "initial preference" not in contents
            path.write_text(
                contents.rstrip() + "\n\n# old note\ninitial preference\n\n"
                "# forgotten note\nforgotten note\n\n"
                "# external edit\nexternal edit marker\n\n",
                encoding="utf-8",
            )

            for _ in range(100):
                page = await asyncio.to_thread(api.list, scope, security=security())
                if any(item.content == "external edit marker" for item in page.items):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("watchdog did not reconcile the edited document")
            assert sorted((item.content, item.lifecycle.value) for item in page.items) == [
                ("corrected preference", "active"),
                ("external edit marker", "active"),
                ("forgotten note", "forgotten"),
                ("initial preference", "superseded"),
            ]
            assert {item.id for item in page.items if item.lifecycle.value != "active"} == {
                initial.id, forgotten.id,
            }
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_fixed_port_conflict_keeps_memory_running(tmp_path, caplog):
    auth = build_dev_authenticator()

    async def run():
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            provider = AgentMemoryMemoryProvider(
                config_dict=document_config(tmp_path),
                security_provider=lambda: internal_context(auth),
                portal={"port": occupied.getsockname()[1]},
            )
            await provider.initialize()
            try:
                assert not provider.portal_status["available"]
                assert provider.runtime is not None
                units = await provider.runtime.api.add_async(
                    "still working",
                    Scope(org="local", user="developer"),
                    security=internal_context(auth),
                )
                assert units
                occupied.close()
                await provider.initialize()
                assert not provider.portal_status["available"]
                assert await provider.open_portal()
                assert provider.portal_status["available"]
            finally:
                await provider.shutdown()

    asyncio.run(run())
    assert "shared Portal exited before becoming ready (exit code 1)" in caplog.text
    assert "[Errno " in caplog.text or "[WinError " in caplog.text


def test_shared_portal_logs_safe_startup_error(tmp_path, caplog):
    from jiuwen_memory_entry.portal import shared

    location = shared.SharedPortalLocation((str(tmp_path), str(tmp_path / "index.db")))
    script = (
        "import sys\n"
        "sys.stderr.write('x' * 131072 + '\\n')\n"
        "sys.stderr.write(\"pywintypes.error: (5, 'CreateNamedPipe', 'private-path')\\n\")\n"
        "sys.exit(7)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE)
    with (
        patch.object(shared.subprocess, "Popen", return_value=process),
        pytest.raises(BackendError, match="shared Portal process could not start"),
    ):
        location.register("0" * 32, {"port": 0, "origins": []})
    assert "exit code 7); stderr: pywintypes.error [WinError 5]" in caplog.text
    assert "private-path" not in caplog.text
    assert "x" * 100 not in caplog.text


def test_shared_portal_logs_ready_timeout(tmp_path, monkeypatch, caplog):
    from jiuwen_memory_entry.portal import shared

    location = shared.SharedPortalLocation((str(tmp_path), str(tmp_path / "index.db")))
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stderr=subprocess.PIPE,
    )
    monkeypatch.setattr(shared, "_START_TIMEOUT", 0.15)
    with (
        patch.object(shared.subprocess, "Popen", return_value=process),
        pytest.raises(BackendError, match="shared Portal process could not start"),
    ):
        location.register("0" * 32, {"port": 0, "origins": []})
    assert "startup timed out after 0.15 s" in caplog.text
    assert process.returncode is not None


def test_personal_search_keeps_exact_session_and_source_before_top_k(tmp_path):
    from jiuwen_memory.api import Context, assemble_runtime

    auth = build_dev_authenticator()
    security = internal_context(auth)
    runtime = assemble_runtime(config=document_config(tmp_path))
    scopes = [Scope(org="local", user="developer", agent="design", session=s) for s in ("a", "b")]
    try:
        expected = {}
        for scope in scopes:
            for source in ("source-one", "source-two"):
                unit = runtime.api.add(
                    f"ocean memory {scope.session} {source}", scope, security=security,
                    system_metadata={"source_app": source},
                )[0]
                expected[unit.id] = (scope, source)

        def search(top_k, source=None):
            context = Context(extensions={"personal": {"source": source}})
            return runtime.api.search("ocean", context, security=security, top_k=top_k).items

        hits = search(10)
        assert len(hits) == len(expected)
        assert len({hit.item.unit_id for hit in hits}) == len(expected)
        assert all(hit.scope == expected[hit.item.unit_id][0] for hit in hits)
        assert {hit.scope.session for hit in hits} == {"a", "b"}
        for source in ("source-one", "source-two"):
            hits = search(2, source)
            assert len(hits) == 2
            assert all(expected[hit.item.unit_id][1] == source for hit in hits)
            assert {hit.scope.session for hit in hits} == {"a", "b"}
    finally:
        runtime.close()


@pytest.mark.parametrize("corrupt_overlay", [False, True])
def test_disabled_sdk_portal_does_not_touch_optional_overlay(tmp_path, corrupt_overlay):
    auth = build_dev_authenticator()
    config = document_config(tmp_path)
    overlay = tmp_path / "memory" / ".shadow" / "portal-config.db"
    original = b"invalid optional overlay"
    if corrupt_overlay:
        overlay.parent.mkdir(parents=True)
        overlay.write_bytes(original)
        overlay.chmod(0o600)

    async def run():
        provider = AgentMemoryMemoryProvider(
            config_dict=config, security_provider=lambda: internal_context(auth),
            portal={"enabled": False},
        )
        try:
            provider.get_tool_schemas()
            runtime = provider.runtime
            assert runtime is not None
            assert runtime.configuration is None
            await provider.initialize()
            assert provider.runtime is runtime
            assert await provider.open_portal() is None
            assert await provider.runtime.api.add_async(
                "disabled portal still writes memory", Scope(org="local", user="developer"),
                security=internal_context(auth),
            )
            if corrupt_overlay:
                assert overlay.read_bytes() == original
            else:
                assert not overlay.exists()
        finally:
            await provider.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("vector", [False, True])
def test_personal_search_with_distractors_beyond_recall_window(tmp_path, vector):
    from jiuwen_memory.api import Context, assemble_runtime

    config = document_config(tmp_path)
    if vector:
        pytest.importorskip("sqlite_vec")
    else:
        config["shadow_index"]["default"]["params"].pop("embedder", None)
    security = internal_context(build_dev_authenticator())
    runtime = assemble_runtime(config=config)
    own = Scope(org="local", user="developer", agent="design", session="a")
    other = Scope(org="local", user="developer", agent="design", session="b")
    try:
        target = runtime.api.add(
            "ocean " + "filler " * 40, own, security=security,
            system_metadata={"source_app": "own"},
        )[0]
        for _index in range(65):
            runtime.api.add("ocean", other, security=security,
                            system_metadata={"source_app": "other"})

        def search(top_k, source=None):
            context = Context(extensions={"personal": {"source": source}})
            return runtime.api.search("ocean", context, security=security, top_k=top_k).items

        assert {hit.scope.session for hit in search(2)} == {"a", "b"}
        # Also protect a source within one session, where scope-only filtering is insufficient.
        for _index in range(65):
            runtime.api.add("ocean", own, security=security,
                            system_metadata={"source_app": "other"})
        for source in ("own", ""):
            if source == "":
                extra = runtime.api.add("ocean faint", own, security=security)[0]
                expected_id = extra.id
            else:
                expected_id = target.id
            hits = search(1, source)
            assert len(hits) == 1 and hits[0].item.unit_id == expected_id
            assert hits[0].scope == own
    finally:
        runtime.close()


@pytest.mark.parametrize("source", ["inline", "file"])
@pytest.mark.parametrize("scene", ["agent", "code"])
def test_sdk_embedded_portal_writes_into_rail_scope(tmp_path, scene, source):
    auth = build_dev_authenticator()

    def security():
        return internal_context(auth)

    async def run():
        config = document_config(tmp_path)
        if scene == "code":
            config["globals"]["write_document"] = False
        options = {"config_dict": config}
        if source == "file":
            path = tmp_path / "memory.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            options = {"config_path": str(path)}
        target = {"instance_id": f"{scene}-{source}"} if os.name in ("posix", "nt") else {}
        provider = AgentMemoryMemoryProvider(
            **options, security_provider=security, scene=scene, project="demo",
            portal=target,
        )
        await provider.initialize(
            scope_id="local", user_id="developer", agent_id="rail", session_id="session",
        )
        try:
            url = await provider.open_portal()
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            token = parse_qs(parts.fragment)["access"][0]

            def use_portal():
                opener = build_opener(ProxyHandler({}))
                headers = {"Host": "portal.example:8080", "Origin": "http://host.example"}
                request = Request(f"{origin}/portal/", headers=headers)
                with opener.open(request, timeout=5) as page:
                    assert page.headers.get("Content-Security-Policy") is None
                    assert page.headers.get("X-Frame-Options") is None

                def post(action, body, session=None):
                    request_headers = {**headers, "Content-Type": "application/json"}
                    if session:
                        request_headers["X-Memory-Portal-Session"] = session
                    request = Request(
                        origin + ("/portal/api/" if action == "bootstrap" else "/v1/") + action,
                        json.dumps(body).encode(), headers=request_headers,
                    )
                    with opener.open(request, timeout=5) as result:
                        return json.load(result)

                session = post("bootstrap", {"token": token, "embedded": True})["session_token"]
                settings = post("read_config", target, session)
                assert settings["available"], settings
                page = post("list", personal_list(), session)
                assert page["add_context"] == {"agent": "rail", "agent_locked": True}
                with pytest.raises(HTTPError) as error:
                    post("add", {"content": "wrong agent", "scope": {"agent": "other"}}, session)
                assert error.value.code == 403
                with pytest.raises(HTTPError) as error:
                    post("add", {
                        "content": "wrong project", "scope": {},
                        "system_metadata": {"project": "../escape"},
                    }, session)
                assert error.value.code == 400
                return post("add", {
                    "content": "ocean project memory", "scope": {},
                    "system_metadata": {"project": "demo"},
                }, session)

            added = await asyncio.to_thread(use_portal)
            assert added[0]["system_metadata"]["project"] == "demo"
            assert added[0]["scope"]["agent"] == "rail"
            tool = ("agent_memory_coding_memory_search" if scene == "code"
                    else "agent_memory_search")
            result = await provider.handle_tool_call(
                tool, {"query": "ocean", "project": "demo"},
            )
            assert "ocean project memory" in result, result
        finally:
            await provider.shutdown()

    asyncio.run(run())
