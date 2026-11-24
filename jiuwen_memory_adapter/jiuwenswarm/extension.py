"""MindVault management page registered in Swarm's Gateway."""

import asyncio
import os
import sqlite3
import sys
from urllib.parse import urlsplit

import yaml
from agent_gateway.auth.service import ModelAuthRequired, live_session
from agent_gateway.enterprise import is_enterprise
from agent_gateway.extensions.sdk import ApplicationPluginExtension, FrontendContribution

from jiuwen_memory_adapter.jiuwenswarm.security_control import (
    StateConflict,
    SwarmProtectionStore,
)
from jiuwen_memory_adapter.jiuwenswarm.security_install import (
    load_memory_config,
    read_installation,
    swarm_infra_dir,
)

_GET = "plugin.mindvault.get"
_SET = "plugin.mindvault.set"
_SET_POLICY = "plugin.mindvault.policy.set"


def _same_origin(ws, configured):
    headers = getattr(ws, "headers", None)
    if headers is None:
        # The WebChannel also supports an HTTP RPC outbound; this app uses browser WS only.
        headers = getattr(getattr(ws, "request", None), "headers", {})
    value = headers.get("origin", "")
    origin = urlsplit(value)
    if origin.scheme not in {"http", "https"} or not origin.netloc:
        return False
    return value == configured if configured else origin.netloc == headers.get("host", "")


def _configured_write_redaction(path):
    if not path:
        return None
    try:
        config = load_memory_config(path)
    except (OSError, RecursionError, TypeError, ValueError, yaml.YAMLError):
        return None
    settings = config.get("globals", {}) if isinstance(config, dict) else None
    if not isinstance(settings, dict):
        return None
    enabled = settings.get("privacy_enabled", False)
    return enabled if isinstance(enabled, bool) else None


class MindVaultApplication(ApplicationPluginExtension):
    plugin_id = "mindvault"

    async def initialize(self, config):
        infra = swarm_infra_dir()
        settings = read_installation(infra)
        self._memory_config = settings.get("memory_config")
        owner = settings.get("owner") or os.getenv("MINDVAULT_SWARM_OWNER_ID", "").strip()
        self._owner = owner if not is_enterprise() else ""
        self._origin = settings.get("origin") or (
            os.getenv("MINDVAULT_SWARM_WEB_ORIGIN", "").rstrip("/")
        )
        self._store = await asyncio.to_thread(
            SwarmProtectionStore, infra / "mindvault" / "control.sqlite3",
        ) if self._owner else None

    async def shutdown(self):
        self._store = None

    def frontend_asset_root(self):
        root = self._get_extension_dir()
        return root / "frontend" if root is not None else None

    def frontend_contributions(self):
        return (FrontendContribution(
            id="mindvault-settings", nav_key="app:mindvault", title="MindVault",
            entrypoint="index.html", position=100,
        ),) if not is_enterprise() else ()

    def bind_web_channel(self, channel, services):
        async def authorize(ws):
            if self._store is None or not _same_origin(ws, self._origin):
                return False
            try:
                session = await asyncio.to_thread(
                    live_session, getattr(ws, "_jiuwen_auth_session", None), False,
                )
            except ModelAuthRequired:
                return False
            return session.user_id == self._owner

        def handler(field=None):
            async def dispatch(ws, req_id, params, _session_id):
                try:
                    if not await authorize(ws):
                        code = "FORBIDDEN"
                        error = "请从 Swarm 使用实例所有者账号登录。"
                    else:
                        if field is not None:
                            if (
                                not isinstance(params, dict)
                                or set(params) != {field, "revision"}
                            ):
                                raise ValueError("invalid settings fields")
                            update = self._store.set_policy if field == "policy" else (
                                self._store.set_enabled
                            )
                            result = await asyncio.to_thread(
                                update, params[field], params["revision"],
                            )
                        else:
                            result = await asyncio.to_thread(self._store.public)
                        configured = await asyncio.to_thread(
                            _configured_write_redaction, self._memory_config,
                        )
                        result["memory_write_redaction"] = {
                            "configured": configured,
                            # A live Worker only publishes after opening the privacy-enabled SDK.
                            "effective": True if result["configuration"] is not None else None,
                        }
                        await channel.send_response(ws, req_id, ok=True, payload=result)
                        return
                except StateConflict:
                    code, error = "STATE_CONFLICT", "设置已变化，请刷新后重试。"
                except ValueError:
                    code, error = "BAD_REQUEST", "保护设置无效。"
                except (OSError, sqlite3.Error):
                    code, error = "UNAVAILABLE", "暂时无法读取保护状态。"
                await channel.send_response(ws, req_id, ok=False, code=code, error=error)

            return dispatch

        channel.register_method(_GET, handler(), local_only=True)
        channel.register_method(_SET, handler("enabled"), local_only=True)
        channel.register_method(_SET_POLICY, handler("policy"), local_only=True)


class MindVaultWorker(ApplicationPluginExtension):
    plugin_id = "mindvault"

    async def initialize(self, config):
        from jiuwen_memory_adapter.jiuwenswarm.security_worker import (
            guard_unavailable_worker,
            start_installed_worker,
        )

        infra = swarm_infra_dir()
        try:
            self._close = await start_installed_worker(read_installation(infra))
        except Exception as exc:
            self._close = guard_unavailable_worker(infra / "mindvault" / "control.sqlite3")
            config.logger.warning("MindVault Worker setup failed (%s)", type(exc).__name__)
            return
        config.logger.info("MindVault protection mounted in resident Swarm Worker")

    async def shutdown(self):
        await self._close()


async def register_extensions(registry):
    if "jiuwenswarm.server.worker.runtime_bootstrap" in sys.modules:
        if is_enterprise():
            return []
        return [MindVaultWorker()]
    plugin = MindVaultApplication()
    registry.register_application_plugin(plugin)
    return [plugin]
