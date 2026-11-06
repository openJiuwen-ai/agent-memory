"""真实框架共用的检测、可信操作及 SDK 来源装配，不替代框架执行。"""

import pytest

from jiuwen_memory_adapter.agent_security import Actor, Hook, SecurityDenied
from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector
from jiuwen_memory_adapter.agent_security.runtime import (
    ApprovalBroker,
    DefaultEgressPolicy,
    EgressGuard,
    Operation,
    PayloadInspector,
)

SAMPLE = "11010519491231002X"
SCHEMA = {
    "type": "object",
    "properties": {"body": {"type": "string"}, "to": {"type": "string"}},
    "required": ["body", "to"],
    "additionalProperties": False,
}


def guard():
    return EgressGuard(
        PayloadInspector(create_text_inspector(), frozenset({"body"})),
        DefaultEgressPolicy(frozenset({"local-collector"})),
        ApprovalBroker(),
        authorize=lambda _: True,
    )


def operation(context, payload):
    if payload.get("to") != "approved":
        raise SecurityDenied("目的地不允许")
    host_session = context.host.session.get_session_id()
    return Operation.create(
        operation_id=context.call_id,
        actor=Actor("test", "alice", host_session, "execution-agent", "1"),
        hook=Hook.BEFORE_TOOL_CALL,
        target="local-collector",
        action=context.name,
        payload=payload,
        source_level="S1",
        source_version="0",
    )


def collector(calls):
    def send(approved, context):
        try:
            approved["body"] = "bypassed"
        except TypeError:
            pass
        else:
            raise AssertionError("批准请求必须不可变")
        calls.append(dict(approved))
        return "sent"

    return send


@pytest.fixture
def sdk_source(tmp_path):
    from jiuwen_memory.api import Scope, assemble_server_runtime
    from jiuwen_memory_adapter.agent_security.memory import SdkMemoryReader
    from jiuwen_memory_adapter.agent_security.sources import SourceLedger

    actor = Actor("test", "alice", "execution-thread", "execution-agent", "1")
    scope = Scope(org=actor.tenant_id, user=actor.user_id)
    runtime = assemble_server_runtime(config=sdk_settings(tmp_path))
    sources, allowed = SourceLedger(), {"value": True}
    sources.begin(actor, fresh=True)
    inspector = create_text_inspector()
    reader = SdkMemoryReader(
        runtime.api,
        sources,
        inspector,
        provider="source-sdk",
        security_for=lambda _: sdk_security(scope),
        scope_for=lambda _: scope,
        authorize_use=lambda *args: allowed["value"],
        detector_version_for=lambda _: inspector.version,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    try:
        unit = runtime.api.add("联系 13800138000", scope, security=sdk_security(scope))[0]
        view = reader.get(unit.id, actor)
        yield runtime, sources, reader, allowed, actor, scope, view
    finally:
        reader.close()
        sources.close()
        runtime.close()


def sdk_security(scope):
    from jiuwen_memory.api import internal_context
    from tests.support.scoped_authenticator import ScopedAuthenticator

    return internal_context(ScopedAuthenticator(scope))


def sdk_settings(tmp_path):
    from jiuwen_memory import api

    # #406 同时交付隐私 SDK 和公开 HumanView 门面；仅缺少这项已知前置时跳过。
    # 已具有该能力的 SDK 若装配或门禁失败，仍应正常报错，不能转换为 skip。
    if not hasattr(api, "HumanView"):
        pytest.skip("敏感记忆集成依赖 jiuwen memory PR #406；上游尚未提供隐私 SDK")
    return {
        "globals": {"privacy_enabled": True, "vector_enabled": False},
        "audit": {"default": "in_memory"},
        "permission": {"default": "allow_all"},
        "privacy": {"default": {"target": "regex", "params": {"privacy_store": "private"}}},
        "privacy_store": {
            "private": {
                "target": "encrypted",
                "params": {
                    "cryptography": "private",
                    "raw_privacy_store": {
                        "target": "kv",
                        "params": {
                            "kv_store": {
                                "target": "sqlite",
                                "params": {"db_path": str(tmp_path / "private.sqlite")},
                            }
                        },
                    },
                },
            }
        },
        "cryptography": {"private": {"target": "local", "params": {"key_provider": "private"}}},
        "key_provider": {"private": {"target": "local", "params": {"key_hex": "11" * 32}}},
    }


@pytest.fixture
def llm_server():
    """真正本地 HTTP 服务；所有网络尝试实际到达此处，无 SDK/transport 替身。"""
    import hashlib
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"calls": [], "failures": {}, "redirects": {}, "responses": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_post(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state["calls"].append(
                {
                    "path": self.path,
                    "body": body,
                    "auth_digest": hashlib.sha256(
                        self.headers.get("authorization", "").encode()
                    ).hexdigest(),
                }
            )
            if self.path in state["redirects"]:
                self.send_response(307)
                self.send_header("location", state["redirects"][self.path])
                self.send_header("content-length", "0")
                self.end_headers()
                return
            respond = state.get("responder")
            custom = respond(body, self.path) if respond else state["responses"].get(self.path)
            if custom is not None:
                self.send_response(custom.get("status", 200))
                self.send_header("content-type", custom["type"])
                chunks = [p.encode() if isinstance(p, str) else p for p in custom["chunks"]]
                self.send_header("content-length", str(sum(len(p) for p in chunks)))
                self.send_header("retry-after", "0")
                self.end_headers()
                for index, chunk in enumerate(chunks):
                    if index == 1 and custom.get("barrier"):
                        ready, release = custom["barrier"]
                        ready.set()
                        if not release.wait(5):
                            return
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return
            failures = state["failures"].get(self.path, 0)
            status = 503 if failures else 200
            if failures:
                state["failures"][self.path] -= 1
                value = {"error": {"message": "local fixture unavailable", "type": "server_error"}}
            else:
                value = {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("retry-after", "0")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    # HTTPServer 以协议方法名分派；实现使用普通 Python 方法，再登记标准库入口。
    Handler.do_POST = Handler.do_post
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def llm_guard(targets):
    return EgressGuard(
        PayloadInspector(create_text_inspector(), frozenset({"messages"})),
        DefaultEgressPolicy(frozenset(targets)),
        ApprovalBroker(),
        authorize=lambda _: True,
    )
