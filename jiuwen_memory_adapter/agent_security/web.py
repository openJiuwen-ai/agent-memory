"""可挂入原应用的同源 ASGI 审批端口；身份完全由宿主 authenticate 提供。"""

import asyncio
import inspect
import json
from importlib.resources import files
from urllib.parse import urlsplit
from uuid import uuid4

from ._validation import has_exact_type
from .runtime import Actor, SecurityDenied, encode_payload


def _origins(values):
    result = frozenset(values)
    if not result:
        raise ValueError("必须配置审批界面的同源地址")
    for origin in result:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
            raise ValueError("审批 Origin 必须是完整且无路径的 HTTP(S) 来源")
        elif parsed.username or parsed.password or parsed.query:
            raise ValueError("审批 Origin 必须是完整且无路径的 HTTP(S) 来源")
        elif parsed.fragment:
            raise ValueError("审批 Origin 必须是完整且无路径的 HTTP(S) 来源")
    return result


async def _body(receive, *, limit=4096):
    chunks, size = [], 0
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        async with asyncio.timeout_at(deadline):
            message = await receive()
        if message["type"] == "http.disconnect":
            raise SecurityDenied("审批连接已断开")
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise SecurityDenied("控制请求过大")
        chunks.append(chunk)
        if not message.get("more_body"):
            break

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SecurityDenied("控制请求含重复字段")
            result[key] = value
        return result

    value = json.loads(b"".join(chunks), object_pairs_hook=pairs)
    encode_payload(value)
    return value


async def _reply(send, status, value, *, kind="application/json; charset=utf-8"):
    data = encode_payload(value).encode() if kind.startswith("application/json") else value
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", kind.encode()),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


def create_approval_app(plugin, *, authenticate, allowed_origins):
    origins = _origins(allowed_origins)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            raise RuntimeError("审批端口只支持 HTTP；生命周期由宿主应用管理")
        path = scope["path"].removeprefix(scope.get("root_path", "")) or "/"
        if path == "/widget.mjs" and scope["method"] == "GET":
            asset = files(__package__).joinpath("web/approval-card.mjs").read_bytes()
            await _reply(send, 200, asset, kind="text/javascript; charset=utf-8")
            return
        try:
            actor = authenticate(scope)
            if inspect.isawaitable(actor):
                actor = await actor
            if not isinstance(actor, Actor):
                raise SecurityDenied("控制入口未认证")
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            if scope["method"] == "POST" and (
                headers.get(b"origin", b"").decode() not in origins
                or headers.get(b"content-type", b"").split(b";")[0] != b"application/json"
            ):
                raise SecurityDenied("控制入口来源或编码不允许")
            if path == "/requests" and scope["method"] == "GET":
                response = {
                    "requests": list(plugin.pending(actor)),
                    "coverage": plugin.coverage().public(),
                }
            elif path == "/decide" and scope["method"] == "POST":
                value = await _body(receive)
                if not has_exact_type(value, dict) or set(value) != {
                    "approval_id",
                    "version",
                    "choice",
                }:
                    raise SecurityDenied("决定必须绑定精确审批版本")
                plugin.decide(actor=actor, **value)
                response = {"accepted": True}
            elif path == "/audit" and scope["method"] == "GET":
                plugin.pending(actor)  # 同一控制会话的所有权检查。
                response = {"events": list(plugin.audit.drain(actor=actor))}
            else:
                await _reply(send, 404, {"error": "unknown security endpoint"})
                return
            await _reply(send, 200, response)
        except Exception:
            await _reply(
                send, 403, {"error": "安全控制请求未接受，请检查当前身份、会话和请求状态。"}
            )

    return app


def create_chat_app(plugin, agent, *, authenticate, allowed_origins):
    """最小可运行宿主示例；生产应用通常只挂载 create_approval_app 和同源组件。"""
    origins = _origins(allowed_origins)
    approvals = create_approval_app(plugin, authenticate=authenticate, allowed_origins=origins)
    runs = {}

    async def actor_for(scope):
        actor = authenticate(scope)
        if inspect.isawaitable(actor):
            actor = await actor
        if not isinstance(actor, Actor) or plugin.owner_for(actor) != actor:
            raise SecurityDenied("会话未认证")
        return actor

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    tasks = [item[1] for item in runs.values() if not item[1].done()]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await plugin.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        path = scope["path"]
        if path.startswith("/security/"):
            child = {**scope, "root_path": "/security"}
            await approvals(child, receive, send)
            return
        try:
            actor = await actor_for(scope)
            if path == "/" and scope["method"] == "GET":
                page = files(__package__).joinpath("web/chat.html").read_bytes()
                await _reply(send, 200, page, kind="text/html; charset=utf-8")
                return
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            if scope["method"] == "POST" and (
                headers.get(b"origin", b"").decode() not in origins
                or headers.get(b"content-type", b"").split(b";")[0] != b"application/json"
            ):
                raise SecurityDenied("会话来源无效")
            if path == "/run" and scope["method"] == "POST":
                body = await _body(receive, limit=65536)
                if (
                    not has_exact_type(body, dict)
                    or set(body) != {"text"}
                    or not has_exact_type(body["text"], str)
                ):
                    raise SecurityDenied("会话只接受文本")
                if len(runs) >= 128 or any(
                    owner == actor and not task.done() for owner, task in runs.values()
                ):
                    raise SecurityDenied("此会话已有待完成操作或容量已满")
                token = uuid4().hex
                task = asyncio.create_task(agent.ainvoke(body["text"], session=actor))
                # Retrieve failures even when a browser disconnects without polling /result.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                runs[token] = (actor, task)
                await _reply(send, 200, {"run_id": token})
            elif path in {"/result", "/cancel"} and scope["method"] == "POST":
                body = await _body(receive)
                if set(body) != {"run_id"} or body["run_id"] not in runs:
                    raise SecurityDenied("运行不存在")
                owner, task = runs[body["run_id"]]
                if owner != actor:
                    raise SecurityDenied("运行会话所有者不匹配")
                if path == "/cancel":
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await _reply(send, 200, {"status": "cancelled"})
                elif not task.done():
                    await _reply(send, 200, {"status": "running"})
                elif task.cancelled() or task.exception() is not None:
                    await _reply(
                        send, 200, {"status": "stopped", "content": "操作已中止或未通过安全检查。"}
                    )
                else:
                    await _reply(send, 200, {"status": "completed", **task.result()})
            else:
                await _reply(send, 404, {"error": "unknown endpoint"})
        except Exception:
            await _reply(send, 403, {"error": "会话请求未接受。"})

    return app
