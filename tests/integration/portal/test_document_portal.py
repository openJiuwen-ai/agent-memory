"""Real Markdown/shadow storage, SDK plugin, local browser authentication and HTTP calls."""

import asyncio
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.cookiejar import CookieJar
from threading import Event
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest

from jiuwen_memory.api import Scope, build_dev_authenticator, internal_context
from jiuwen_memory.storage.sync_gate import document_sync_gate
from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
    AgentMemoryMemoryProvider,
    _agent_default_config,
)

pytestmark = pytest.mark.integration


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


def document_config(root, *, watch=False):
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
    def __init__(self, url):
        parts = urlsplit(url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.token = parse_qs(parts.fragment)["access"][0]
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        assert self.call("bootstrap", {"token": self.token})[0] == 200

    def call(self, action, body, *, origin=None):
        request = Request(
            self.origin + ("/portal/api/" if action == "bootstrap" else "/v1/") + action,
            json.dumps(body).encode(),
            headers={
                "Origin": origin or self.origin, "Content-Type": "application/json",
                "X-Memory-Portal-Browser": "1",
            },
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)


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

        def call(name, body):
            return asyncio.to_thread(browser.call, name, body)

        try:
            assert (await call("bootstrap", {"token": browser.token}))[0] == 401
            assert (
                await asyncio.to_thread(browser.call, "list", personal_list(), origin="http://evil.invalid")
            )[0] == 404
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
            status, settings = await call("read_config", {})
            assert status == 200 and settings["available"], settings
            assert settings["instance"]["document_root"] == str(document_root)
            status, saved = await call(
                "save_config",
                {
                    "version": settings["version"],
                    "changes": {
                        "llm.default.params.llm_model": "saved",
                        "llm.default.params.llm_api_key": "synthetic-secret",
                    },
                },
            )
            assert status == 200 and saved["applied"], saved
            status, settings = await call("read_config", {})
            assert status == 200 and settings["apply_state"] == "applied"
            assert "synthetic-secret" not in json.dumps(settings)
            await provider.initialize()
            assert provider.runtime is runtime
            assert urlsplit(provider.portal_url).port == port
        finally:
            await provider.shutdown()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
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
            status, settings = await asyncio.to_thread(second.call, "read_config", {})
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
            status, settings = await asyncio.to_thread(first.call, "read_config", {})
            assert status == 200 and settings["available"], settings
            version = settings["version"]
            status, saved = await asyncio.to_thread(
                second.call,
                "save_config",
                {
                    "version": version,
                    "changes": {"llm.default.params.llm_model": "second"},
                },
            )
            assert status == 200 and saved["applied"], saved
            status, conflict = await asyncio.to_thread(
                first.call,
                "save_config",
                {
                    "version": version,
                    "changes": {"llm.default.params.llm_model": "first"},
                },
            )
            assert status == 409, conflict
            status, latest = await asyncio.to_thread(first.call, "read_config", {})
            assert status == 200 and latest["version"] == version + 1, latest
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(
                    executor.map(
                        lambda browser: browser.call(
                            "save_config",
                            {
                                "version": version + 1,
                                "changes": {"llm.default.params.llm_temperature": 0.5},
                            },
                        ),
                        [first, second],
                    )
                )
            assert sorted(status for status, _ in outcomes) == [200, 409], outcomes
            status, after = await asyncio.to_thread(first.call, "read_config", {})
            assert status == 200 and after["version"] == version + 2, after
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_correction_invalidates_inflight_watcher_snapshot(
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
        gate = document_sync_gate(str(tmp_path))
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
        try:
            assert snapshot_read.wait(5)
            corrected = invoke_browser_api(
                runtime.api, "update",
                {"scope": vars(scope), "unit_id": old.id, "patch": {"content": "new content"}},
                security=security,
            )
            version_after_correction = gate.snapshot_version()
            assert version_after_correction is not None
        finally:
            resume.set()

        for _ in range(100):
            version = gate.snapshot_version()
            if version is not None and version > version_after_correction:
                break
            time.sleep(0.05)
        else:
            pytest.fail("watchdog did not commit after the correction")

        items = runtime.api.list(scope, security=security).items
        assert sorted((item.content, item.lifecycle.value) for item in items) == [
            (memory_content(corrected), "active"), ("old content", "superseded"),
        ]
    finally:
        runtime.close()


def test_watcher_commit_does_not_block_writes_in_another_document_root(
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
    committing, release = Event(), Event()
    try:
        original = first.api.add("first content", scope, security=security)[0]
        path = first_root / original.system_metadata["md_filename"]
        watchdog, = document_watchdogs
        gate = document_sync_gate(str(first_root))
        commit = gate.commit_sync

        @contextmanager
        def hold_commit(version):
            with commit(version) as accepted:
                if accepted:
                    committing.set()
                    assert release.wait(10)
                yield accepted

        monkeypatch.setattr(gate, "commit_sync", hold_commit)
        first.start()
        watchdog.schedule_sync(str(path))
        with ThreadPoolExecutor(1) as pool:
            try:
                assert committing.wait(5)
                written = pool.submit(second.api.add, "second content", scope, security=security)
                assert written.result(timeout=5)
            finally:
                release.set()
    finally:
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


def test_fixed_port_conflict_keeps_memory_running(tmp_path, monkeypatch):
    from jiuwen_memory_entry.portal import hosting

    auth = build_dev_authenticator()
    start_portal = hosting.start_portal
    attempts = []

    def count_start(*args, **kwargs):
        attempts.append(kwargs["port"])
        return start_portal(*args, **kwargs)

    monkeypatch.setattr(hosting, "start_portal", count_start)

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
                await provider.initialize()
                assert len(attempts) == 1
                assert provider.runtime is not None
                units = await provider.runtime.api.add_async(
                    "still working",
                    Scope(org="local", user="developer"),
                    security=internal_context(auth),
                )
                assert units
                occupied.close()
                assert await provider.open_portal()
                assert len(attempts) == 2
            finally:
                await provider.shutdown()

    asyncio.run(run())


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


def test_sdk_embedded_portal_writes_into_rail_scope(tmp_path):
    auth = build_dev_authenticator()

    def security():
        return internal_context(auth)

    async def run():
        parent = "http://localhost:8080"
        provider = AgentMemoryMemoryProvider(
            config_dict=document_config(tmp_path), security_provider=security,
            portal={"allowed_frame_origins": [parent]},
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
                with opener.open(f"{origin}/portal/", timeout=5) as page:
                    assert page.headers["Content-Security-Policy"] == f"frame-ancestors {parent}"
                    assert page.headers.get("X-Frame-Options") is None

                def post(action, body, session=None):
                    headers = {"Origin": origin, "Content-Type": "application/json"}
                    if session:
                        headers["X-Memory-Portal-Session"] = session
                    request = Request(
                        origin + ("/portal/api/" if action == "bootstrap" else "/v1/") + action,
                        json.dumps(body).encode(), headers=headers,
                    )
                    with opener.open(request, timeout=5) as result:
                        return json.load(result)

                session = post("bootstrap", {"token": token, "embedded": True})["session_token"]
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
            result = await provider.handle_tool_call(
                "agent_memory_search", {"query": "ocean", "project": "demo"},
            )
            assert "ocean project memory" in result, result
        finally:
            await provider.shutdown()

    asyncio.run(run())
