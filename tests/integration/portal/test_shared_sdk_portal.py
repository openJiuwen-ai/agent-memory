"""Shared SDK Portal outlives individual Providers and drains before the Runtime."""

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml

from jiuwen_memory.api import PermissionDeniedError, Scope, ValidationError, internal_context
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


@pytest.mark.parametrize("first_to_close", [0, 1])
def test_closing_one_provider_preserves_browser_session_and_portal(tmp_path, first_to_close):
    async def run():
        config = sdk_config(tmp_path)

        def security():
            return internal_context(ScopedAuthenticator(Scope(org="demo", user="owner")))

        providers = [AgentMemoryMemoryProvider(
            config_dict=config, continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=security,
        ) for _ in range(2)]
        try:
            await asyncio.gather(*(p.initialize(scope_id="demo") for p in providers))
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

        provider = AgentMemoryMemoryProvider(
            config_dict=sdk_config(tmp_path), continuity_enabled=True, continuity_mode="continuity",
            user_id="owner", security_provider=security,
        )
        try:
            await provider.initialize(scope_id="demo")
            old_portal = start_portal(provider.runtime, security_provider=security)
            await asyncio.to_thread(old_portal.close)
            assert not provider.portal_status["available"]
            assert provider.portal_status["address"] is None
            assert await provider.open_portal()
            reopened = start_portal(provider.runtime, security_provider=security)
            assert reopened is not old_portal
            await provider.shutdown()
            assert reopened.closed
        finally:
            await provider.shutdown()
    asyncio.run(run())
