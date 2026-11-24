"""Personal Worker assembly using the native owner, Swarm model and explicit MCP allowlist."""

import asyncio
import re
import secrets
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit

from ..agent_security import Actor, EgressPolicy, LlmTarget, SecurityPlugin, TargetRule
from ..agent_security.backend_jiuwen import JiuwenMemoryBackend
from ..agent_security.backends import MemoryBackendRegistry
from ..agent_security.hooks import Hook
from ..agent_security.inspection import TextInspection
from ..agent_security.llm import chat_endpoint, normalize_llm_endpoint
from ..agent_security.runtime import SecurityDenied
from .security_control import SwarmProtectionStore
from .security_host import SwarmSessionPorts, install_swarm_work_code, start_swarm_protection
from .security_install import load_memory_config
from .security_messages import MEMORY_PROVIDER

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _MemoryEgressInspector:
    def __init__(self, scanner, used_memory):
        self._scanner, self._used_memory = scanner, used_memory
        self.version = scanner.version + ":swarm-memory-use"

    def inspect(self, text, *, label=""):
        if self._used_memory():
            return self._scanner.inspect(text, label=label)
        return TextInspection("S1", text)


def _https_or_local_http(endpoint):
    if endpoint.scheme == "https":
        return True
    return endpoint.scheme == "http" and endpoint.hostname in _LOOPBACK_HOSTS


@asynccontextmanager
async def _http_streams(endpoint):
    import httpx
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx.AsyncClient(follow_redirects=False, trust_env=False) as http,
        streamable_http_client(endpoint, http_client=http) as (read, write, _),
    ):
        yield read, write


class _McpConnection:
    def __init__(self, streams):
        self.streams = streams
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        from mcp import ClientSession

        try:
            async with (
                self.streams() as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                self.ready.set_result(client)
                await self.stop.wait()
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)

    async def aclose(self):
        self.stop.set()
        if not self.ready.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def _mcp_endpoints(allowed, entries):
    urls = {}
    for entry in entries:
        name = entry.get("name")
        if name not in allowed or entry.get("enabled") is False:
            continue
        url = entry.get("url") or entry.get("server_url")
        parsed = urlsplit(url or "")
        if name in urls or not parsed.hostname or not _https_or_local_http(parsed):
            raise SecurityDenied("MindVault MCP 需要无自定义认证的 Streamable HTTP 地址")
        if any((parsed.username, parsed.password, parsed.query, parsed.fragment)):
            raise SecurityDenied("MindVault MCP 需要无自定义认证的 Streamable HTTP 地址")
        if entry.get("headers"):
            raise SecurityDenied("MindVault MCP 需要无自定义认证的 Streamable HTTP 地址")
        urls[name] = url
    if set(urls) != set(allowed):
        raise SecurityDenied("MindVault MCP 允许列表与 Swarm 配置不一致")
    return urls


def _swarm_model():
    from jiuwenswarm.common.config import get_default_models
    from jiuwenswarm.runtime.model_catalog import build_model_catalog

    entries = get_default_models()
    current = build_model_catalog(entries).current_selection
    if not current:
        raise SecurityDenied("Swarm 未配置默认模型")
    config = entries[int(current.rsplit("#", 1)[1])]["model_client_config"]
    unsupported = "MindVault 需要 OpenAI Chat Completions API Key 模型"
    provider = config.get("client_provider", "OpenAI")
    auth_mode = config.get("auth_mode", "api_key")
    api_mode = config.get("api_mode")
    if provider != "OpenAI" or auth_mode != "api_key" or api_mode not in {None, "chat_completions"}:
        raise SecurityDenied(unsupported)
    api_key = config.get("api_key")
    if config.get("custom_headers") or not api_key:
        raise SecurityDenied(unsupported)
    base = normalize_llm_endpoint(config.get("api_base", "")).rstrip("/")
    target = urlsplit(base)
    if not _https_or_local_http(target):
        raise SecurityDenied("远程 Swarm 模型要求 HTTPS")
    endpoint = base if target.path.endswith("/chat/completions") else chat_endpoint(base)
    return LlmTarget("swarm-model", provider, config.get("model_name"), endpoint), api_key


def _open_memory(path, scope):
    from jiuwen_memory.api import assemble_server_runtime, register_trusted_principal

    config = load_memory_config(path)
    if not isinstance(config, dict) or config.get("globals", {}).get("privacy_enabled") is not True:
        raise SecurityDenied("MindVault 需要开启隐私保护的 jiuwen memory 配置")
    if config.get("privacy", {}).get("default", {}).get("target") != "regex":
        raise SecurityDenied("MindVault 首版需要 jiuwen memory 的 regex 脱敏配置")
    key = secrets.token_urlsafe(32)
    config["security"] = {"default": {"target": "standard", "params": {
        "authenticator": "mindvault_gateway", "authorizer": "default",
    }}}
    config["authenticator"] = {"mindvault_gateway": {"target": "trusted", "params": {
        "key_store": "mindvault_principals", "gateway_key": key,
    }}}
    config["key_store"] = {"mindvault_principals": {"target": "memory"}}
    config["authorizer"] = {"default": {"target": "standard", "params": {
        "grant_store": "default", "delegation_store": "default",
    }}}
    config["grant_store"] = {"default": {"target": "memory"}}
    config["delegation_store"] = {"default": {"target": "memory"}}
    runtime = assemble_server_runtime(config=config)
    try:
        register_trusted_principal(runtime.security_runtime.authenticator, scope)
    except BaseException:
        runtime.close()
        raise
    return runtime, key


async def start_installed_worker(settings):
    from agent_gateway.auth.service import ModelAuthRequired, live_session
    from jiuwenswarm.common.config import get_config_yaml_mcp_servers
    from jiuwenswarm.common.utils import get_user_workspace_dir

    from jiuwen_memory.api import Credentials, Scope

    owner = settings["owner"]
    scope = Scope(org="local", user=owner)
    target, model_key = _swarm_model()
    endpoints = _mcp_endpoints(settings["mcp"], get_config_yaml_mcp_servers())
    ports = AsyncExitStack()
    clients = {}
    identity = secrets.token_hex(16)
    try:
        runtime, key = await asyncio.to_thread(_open_memory, settings["memory_config"], scope)
        ports.callback(runtime.close)
        memory_creds = Credentials(api_key=key, headers={
            "x-org-id": scope.org, "x-principal-type": "user", "x-principal-id": scope.user,
        })

        async def authenticate(request, mode):
            metadata = request.metadata or {}
            sid = request.session_id
            if request.channel_id != "web" or not isinstance(sid, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", sid
            ):
                raise SecurityDenied("Swarm 仅接受已登录所有者的 Web 文本会话")
            try:
                live = await asyncio.to_thread(live_session, metadata.get("auth_session"), False)
            except ModelAuthRequired:
                raise SecurityDenied("Swarm 登录会话无效") from None
            if live.user_id != owner:
                raise SecurityDenied("Swarm 会话所有者不匹配")
            return Actor("local", owner, sid, "swarm-" + mode, identity)

        async def connect(name, streams):
            if name not in clients or clients[name].task.done():
                connection = clients[name] = _McpConnection(streams)
                ports.push_async_callback(connection.aclose)
            async with asyncio.timeout(10):
                return await asyncio.shield(clients[name].ready)

        async def open_session(actor, _mode, notify, *, policy):
            allowed = {target.target_id, "current-user", *endpoints}
            agent_targets = {f"agent:{actor.agent_id}", "agent:swarm-subagent"}
            if allowed.intersection(agent_targets):
                raise SecurityDenied("Swarm MCP 名称与子 Agent 保留目标冲突")

            def has_memory_reference(snapshot):
                return any(ref.provider == MEMORY_PROVIDER for ref in snapshot.references)

            def allow_transfer(sender, recipient, snapshot):
                return (
                    sender.user_id == recipient.user_id == owner
                    and plugin.owner_for(sender) == plugin.owner_for(recipient)
                    and (policy.agents or not has_memory_reference(snapshot))
                )

            scanner = policy.inspector()
            backend_registry = MemoryBackendRegistry()
            backend_registry.register(
                "jiuwen", lambda context, **options: JiuwenMemoryBackend(
                    replace(context, inspector=scanner), **options,
                ),
            )

            def has_memory_source(who):
                return has_memory_reference(plugin.sources.snapshot(who))

            def used_memory():
                return has_memory_source(plugin.current_run().actor)

            def authorize(op):
                if op.actor.user_id != owner or op.target not in (allowed | agent_targets):
                    return False
                if op.hook == Hook.BEFORE_TOOL_CALL and not policy.tools:
                    return not has_memory_source(op.actor)
                return True

            plugin = SecurityPlugin(
                {"framework": "openjiuwen", "memory_backend": "jiuwen"},
                inspector=_MemoryEgressInspector(scanner, used_memory),
                backend_registry=backend_registry,
                targets=[target],
                policy=EgressPolicy([
                    *(TargetRule(name) for name in sorted(allowed)),
                    *(TargetRule(name, hooks=frozenset({Hook.BEFORE_AGENT_MESSAGE}))
                      for name in sorted(agent_targets)),
                ]),
                authorize=authorize,
                authorize_input=lambda who, _hook, _source: who.user_id == owner,
                authorize_transfer=allow_transfer, notify=notify,
            )
            try:
                backend = plugin.connect_memory_backend(
                    api=runtime.api, provider=MEMORY_PROVIDER, scope_for=lambda _: scope,
                    security_for=lambda _: runtime.authenticate(memory_creds),
                    authorize_use=lambda who, *_: policy.model and who.user_id == owner,
                    protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
                )
                return SwarmSessionPorts(
                    plugin, plugin.model(target.target_id, api_key=model_key), backend.reader,
                    str(get_user_workspace_dir()), "你是 Swarm 助手。按用户要求使用已授权的工具。",
                    recall_memory=policy.model,
                )
            except BaseException:
                await plugin.aclose()
                raise

        async def mcp_for(_actor, _mode, names):
            if any(name not in endpoints for name in names):
                raise SecurityDenied("MCP 未经 MindVault 实例配置授权")
            selected = []
            for name in names:
                client = await connect(name, lambda name=name: _http_streams(endpoints[name]))
                selected.append((name, name, client))
            return selected

        close = await start_swarm_protection(
            authenticate=authenticate, open_session=open_session, mcp_for=mcp_for,
            native_product=True, configure_policy=True,
        )
        ports.push_async_callback(close)
        return ports.aclose
    except BaseException:
        await ports.aclose()
        raise


def guard_unavailable_worker(state_path):
    """Keep enabled sessions closed if the native plugin loader catches setup errors."""
    class Unavailable:
        @property
        def enabled(self):
            try:
                desired, _ = SwarmProtectionStore(state_path).desired()
            except (OSError, sqlite3.Error):
                raise SecurityDenied("无法确认 Swarm 保护设置") from None
            if desired:
                raise SecurityDenied("Swarm 保护未装配，已拒绝执行")
            return False

    uninstall = install_swarm_work_code(
        authenticate=None, open_session=None, mcp_for=None, control=Unavailable(),
    )

    async def close():
        uninstall()

    return close
