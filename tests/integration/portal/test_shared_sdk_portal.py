"""Shared SDK Portal outlives individual Providers and drains before the Runtime."""

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml

from jiuwen_memory.api import (
    PermissionDeniedError,
    Scope,
    SpaceSpec,
    ValidationError,
    internal_context,
)
from jiuwen_memory.common.security.types import Role
from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import AgentMemoryMemoryProvider
from jiuwen_memory_entry.portal import hosting
from jiuwen_memory_entry.portal.hosting import start_portal
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.integration


def sdk_config(tmp_path):
    config = yaml.safe_load(Path("examples/role_memory/server.yaml").read_text(encoding="utf-8"))
    for name, spec in config["kv_store"].items():
        spec["params"]["db_path"] = str(tmp_path / f"{name}.db")
    config["permission"]["default"]["params"]["db_path"] = str(tmp_path / "permissions.db")
    config["continuity"]["default"]["params"] = {
        "mode": "continuity", "single_writer": True, "registry_store": "registry",
    }
    config["globals"]["continuity_connectors"] = {
        "host": {"actor": asdict(Scope(org="demo", user="owner")),
                 "projects": ["role-project"], "enabled": True},
    }
    return config


def role_config(tmp_path):
    config = yaml.safe_load(
        Path("examples/role_memory/server.yaml").read_text(encoding="utf-8")
    )
    for name, spec in config["kv_store"].items():
        spec["params"]["db_path"] = str(tmp_path / f"{name}.db")
    config["permission"]["default"]["params"]["db_path"] = str(tmp_path / "permissions.db")
    config["globals"]["watch_document"] = False
    return config


@pytest.mark.parametrize("config_source", ["dict", "path"])
def test_standard_role_config_exposes_runtime_mode_and_narrow_scope(
    tmp_path, config_source,
):
    async def run():
        actor = Scope(org="demo", user="owner")

        def security():
            return internal_context(ScopedAuthenticator(actor))

        config = role_config(tmp_path)
        if config_source == "path":
            config_path = tmp_path / "server.yaml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            config_options = {"config_path": str(config_path)}
        else:
            config_options = {"config_dict": config}

        provider = AgentMemoryMemoryProvider(
            **config_options,
            user_id="owner",
            security_provider=security,
            portal={"multi_agent": {
                "scope": {
                    "org": "demo", "space": "role-project", "agent": "worker",
                },
                "project_name": "Role Project",
                "connectors": {"host": "Host"},
            }},
        )
        try:
            await provider.initialize(scope_id="demo")
            provider.runtime.api.create_space(
                SpaceSpec(org="demo", space="role-project", owner=actor),
                security=internal_context(ScopedAuthenticator(actor, role=Role.ADMIN)),
            )
            launch = urlsplit(await provider.open_portal())
            origin = f"{launch.scheme}://{launch.netloc}"
            headers = {"Origin": origin, "X-Memory-Portal-Browser": "1"}
            async with httpx.AsyncClient(
                base_url=origin, headers=headers, trust_env=False, timeout=5,
            ) as browser:
                response = await browser.post(
                    "/portal/api/bootstrap",
                    json={"token": parse_qs(launch.fragment)["access"][0]},
                )
                assert response.status_code == 200
                response = await browser.post(
                    "/v1/portal_context", json={},
                    headers={"X-Memory-Portal-View": "multi-agent"},
                )
                assert response.status_code == 200, response.text
                context = response.json()
                assert context["mode"] == "role"
                assert context["scope"]["agent"] == "worker"
                assert context["role_catalog"]["available"] is True
        finally:
            await provider.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("first_to_close", [0, 1])
def test_closing_one_provider_preserves_browser_session_and_portal(tmp_path, first_to_close):
    async def run():
        config = sdk_config(tmp_path)
        portal_options = {
            "multi_agent": {
                "enabled": True,
                "project_name": "Role Project",
                "scope": {"org": "demo", "space": "role-project"},
                "connectors": {"host": "Host"},
            },
        }

        def security():
            return internal_context(ScopedAuthenticator(Scope(org="demo", user="owner")))

        providers = [AgentMemoryMemoryProvider(
            config_dict=config, continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=security,
            portal=portal_options,
        ) for _ in range(2)]
        try:
            await asyncio.gather(*(p.initialize(scope_id="demo") for p in providers))
            actor = Scope(org="demo", user="owner")
            providers[0].runtime.api.create_space(
                SpaceSpec(org="demo", space="role-project", owner=actor),
                security=internal_context(ScopedAuthenticator(actor, role=Role.ADMIN)),
            )
            first, remaining = providers[first_to_close], providers[1 - first_to_close]
            assert first.runtime is remaining.runtime
            assert first.portal_status["address"] == remaining.portal_status["address"]
            portal = start_portal(first.runtime, security_provider=security)
            launch = urlsplit(await remaining.open_portal())
            origin = f"{launch.scheme}://{launch.netloc}"
            headers = {"Origin": origin, "X-Memory-Portal-Browser": "1"}
            async with httpx.AsyncClient(
                base_url=origin, headers=headers, trust_env=False, timeout=5,
            ) as browser:
                response = await browser.post(
                    "/portal/api/bootstrap",
                    json={"token": parse_qs(launch.fragment)["access"][0]},
                )
                assert response.status_code == 200
                await first.shutdown()
                assert not portal.closed
                assert remaining.portal_status["available"]
                assert await remaining.open_portal()
                # An existing browser session must survive, not just a newly issued link.
                response = await browser.post(
                    "/v1/list", json={"extensions": {"personal": {}}},
                )
                assert response.status_code == 200, response.text
                context = await browser.post(
                    "/v1/portal_context", json={},
                    headers={"X-Memory-Portal-View": "multi-agent"},
                )
                assert context.status_code == 200, context.text
                assert context.json()["mode"] == "continuity"
                assert context.json()["role_catalog"] == {
                    "available": False,
                    "reason": "role_mode_required",
                    "policy_version": None,
                    "default_duty": None,
                    "duties": [],
                    "relation_types": [],
                }
                await first.shutdown()
            await remaining.shutdown()
            assert portal.closed
            assert not remaining.portal_status["available"]
        finally:
            await asyncio.gather(*(p.shutdown() for p in providers))
    asyncio.run(run())


def test_browser_session_keeps_the_credentials_of_its_own_provider(tmp_path):
    """Shared actor must not let a lower-privilege Provider borrow another's context."""

    async def run():
        config = sdk_config(tmp_path)
        actor = Scope(org="demo", user="owner")
        calls = {"root": 0, "user": 0, "peer": 0}

        def root_security():
            calls["root"] += 1
            return internal_context(ScopedAuthenticator(actor, role=Role.ROOT))

        def user_security():
            calls["user"] += 1
            return internal_context(ScopedAuthenticator(actor, role=Role.USER))

        def peer_security():
            calls["peer"] += 1
            # Same signed claims as user_security, but a distinct trusted source.
            return internal_context(ScopedAuthenticator(actor, role=Role.USER))

        first = AgentMemoryMemoryProvider(
            config_dict=config, continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=root_security,
        )
        second = AgentMemoryMemoryProvider(
            config_dict=config, continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=user_security,
        )
        third = AgentMemoryMemoryProvider(
            config_dict=config, continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=peer_security,
        )
        try:
            # Distinct roles and a distinct callback with identical claims attach to one Portal.
            await first.initialize(scope_id="demo")
            await second.initialize(scope_id="demo")
            await third.initialize(scope_id="demo")
            assert first.runtime is second.runtime
            assert second.runtime is third.runtime
            launch = urlsplit(await third.open_portal())
            origin = f"{launch.scheme}://{launch.netloc}"
            headers = {"Origin": origin, "X-Memory-Portal-Browser": "1"}
            calls["root"] = calls["user"] = calls["peer"] = 0
            async with httpx.AsyncClient(
                base_url=origin, headers=headers, trust_env=False, timeout=5,
            ) as browser:
                response = await browser.post(
                    "/portal/api/bootstrap",
                    json={"token": parse_qs(launch.fragment)["access"][0]},
                )
                assert response.status_code == 200
                response = await browser.post(
                    "/v1/list", json={"extensions": {"personal": {}}},
                )
                assert response.status_code == 200, response.text
                # Closing the root Provider must not invalidate the user session.
                await first.shutdown()
                response = await browser.post(
                    "/v1/list", json={"extensions": {"personal": {}}},
                )
                assert response.status_code == 200, response.text
            # Identical claims must not merge distinct credential source callbacks.
            assert calls["peer"] >= 1
            assert calls["user"] == 0
            assert calls["root"] == 0
        finally:
            await asyncio.gather(first.shutdown(), second.shutdown(), third.shutdown())

    asyncio.run(run())


def test_default_launch_binds_source_before_registration_changes(monkeypatch):
    actor = Scope(org="demo", user="owner")
    observed_roles = []

    def user_security():
        return internal_context(ScopedAuthenticator(actor, role=Role.USER))

    def root_security():
        return internal_context(ScopedAuthenticator(actor, role=Role.ROOT))

    def browser_api(_api, _verb, _payload, *, security, **_kwargs):
        observed_roles.append(security.auth.role)
        return {"role": security.auth.role.value}

    monkeypatch.setattr(hosting, "invoke_browser_api", browser_api)
    runtime = SimpleNamespace(api=object())
    portal = start_portal(runtime, security_provider=user_security, identity="user")
    try:
        launch = urlsplit(portal.launch_url())
        origin = f"{launch.scheme}://{launch.netloc}"
        headers = {"Origin": origin, "X-Memory-Portal-Browser": "1"}
        with httpx.Client(base_url=origin, headers=headers, trust_env=False, timeout=5) as browser:
            response = browser.post(
                "/portal/api/bootstrap", json={"token": parse_qs(launch.fragment)["access"][0]},
            )
            assert response.status_code == 200
            assert browser.post("/v1/list", json={}).json() == {"role": "user"}
            assert start_portal(
                runtime, security_provider=root_security, identity="root",
            ) is portal
            with pytest.raises(ValidationError, match="provider identity"):
                portal.launch_url()
            # Registration must not invalidate or change an already-issued default grant.
            response = browser.post("/v1/list", json={})
            assert response.status_code == 200
            assert response.json() == {"role": "user"}
            portal.unregister_provider("user")
            assert browser.post("/v1/list", json={}).status_code == 401
            with pytest.raises(PermissionDeniedError, match="not registered"):
                portal.launch_url(identity="user")

        # The sole remaining source can issue its own default link, with root privileges.
        root_launch = urlsplit(portal.launch_url())
        with httpx.Client(base_url=origin, headers=headers, trust_env=False, timeout=5) as browser:
            response = browser.post(
                "/portal/api/bootstrap",
                json={"token": parse_qs(root_launch.fragment)["access"][0]},
            )
            assert response.status_code == 200
            assert browser.post("/v1/list", json={}).json() == {"role": "root"}
            # Old/unbound grants must never fall back to that root source either.
            for grant in (None, {"identity": None, "write_scope": None}):
                response = browser.post(
                    "/portal/api/bootstrap", json={"token": portal.sessions.issue(grant)},
                )
                assert response.status_code == 200
                assert browser.post("/v1/list", json={}).status_code == 401
        assert observed_roles == [Role.USER, Role.USER, Role.ROOT]
        portal.unregister_provider("root")
        with pytest.raises(ValidationError, match="provider identity"):
            portal.launch_url()
    finally:
        portal.close()


def test_closed_portal_is_reported_unavailable_and_can_be_reopened(tmp_path):
    async def run():
        def security():
            return internal_context(ScopedAuthenticator(Scope(org="demo", user="owner")))

        path = tmp_path / "memory.yaml"
        path.write_text(yaml.safe_dump(sdk_config(tmp_path)), encoding="utf-8")
        other_path = tmp_path / "other.yaml"
        other_path.write_bytes(path.read_bytes())
        provider = AgentMemoryMemoryProvider(
            config_path=str(path), continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=security,
        )
        other = AgentMemoryMemoryProvider(
            config_path=str(other_path), continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=security,
        )
        try:
            await provider.initialize(scope_id="demo")
            old_portal = start_portal(provider.runtime, security_provider=security)
            model_key = "llm.default.params.llm_model"
            assert provider.runtime.configuration.save(
                0, {model_key: "restored-model"}, security=security(),
            ) == {"version": 1, "applied": True}
            await other.initialize(scope_id="demo")
            assert other.runtime is provider.runtime
            link = urlsplit(await other.open_portal())
            origin = f"{link.scheme}://{link.netloc}"
            async with httpx.AsyncClient(
                base_url=origin, headers={"Origin": origin}, trust_env=False, timeout=5,
            ) as browser:
                response = await browser.post(
                    "/portal/api/bootstrap", json={"token": parse_qs(link.fragment)["access"][0]},
                )
                assert response.status_code == 200
                response = await browser.post("/v1/list", json={"extensions": {"personal": {}}})
                assert response.status_code == 200
                response = await browser.post("/v1/read_config", json={})
                assert response.status_code == 409 and "配置库" in response.json()["message"]
                response = await browser.post("/v1/save_config", json={
                    "version": 1, "changes": {model_key: "wrong-target"},
                })
                assert response.status_code == 409
            await other.shutdown()
            await asyncio.to_thread(old_portal.close)
            assert not provider.portal_status["available"]
            assert provider.portal_status["address"] is None
            assert await provider.open_portal()
            reopened = start_portal(provider.runtime, security_provider=security)
            assert reopened is not old_portal
            await provider.shutdown()
            assert reopened.closed
            await provider.initialize(scope_id="demo")
            settings = provider.runtime.configuration.read(security=security())
            assert settings["applied_version"] == 1
            assert next(field["effective"] for field in settings["fields"]
                        if field["key"] == model_key) == "restored-model"
            await provider.shutdown()
            await other.initialize(scope_id="demo")
            assert await other.open_portal()
            assert other.runtime.configuration.read(security=security())["version"] == 0
        finally:
            await other.shutdown()
            await provider.shutdown()
    asyncio.run(run())
