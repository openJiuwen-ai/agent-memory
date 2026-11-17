"""HTTPX 公开 transport 扩展；不探测或替换第三方客户端私有字段。"""

import asyncio
import copy
import hashlib
import hmac
import importlib
import json
import secrets
from urllib.parse import urlsplit

from ._validation import has_exact_type
from .runtime import SecurityDenied, encode_payload

_CREDENTIAL_HEADERS = {
    "authorization",
    "api-key",
    "x-api-key",
    "openai-organization",
    "openai-project",
}
_STANDARD_HEADERS = {
    "host",
    "content-type",
    "content-length",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-async",
    "x-stainless-retry-count",
    "x-stainless-read-timeout",
    "x-stainless-timeout",
    "x-stainless-helper-method",
    "x-stainless-raw-response",
}


def _decode(request, adapter, credential_hashes, secret, target_ids):
    if (
        request.method != "POST"
        or request.headers.get("content-type", "").split(";")[0].lower() != "application/json"
        or request.headers.get("content-encoding", "identity") != "identity"
    ):
        raise SecurityDenied("模型请求方法、编码或扩展未覆盖")
    elif set(request.extensions) - {"timeout"}:
        raise SecurityDenied("模型请求方法、编码或扩展未覆盖")
    authority = urlsplit(str(request.url)).netloc
    if request.headers.get("host", "").lower() != authority.lower():
        raise SecurityDenied("模型请求 Host 与实际 URL 不一致")
    for key, expected in credential_hashes.items():
        actual = hmac.new(secret, request.headers.get(key, "").encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(actual, expected):
            raise SecurityDenied("模型客户端认证或账户配置改变")
    for key, value in request.headers.multi_items():
        if key in _CREDENTIAL_HEADERS:
            if key not in credential_hashes:
                raise SecurityDenied("未登记的模型认证或账户头")
            continue
        if key not in _STANDARD_HEADERS:
            raise SecurityDenied("自定义模型请求头未覆盖")
        result = adapter.guard.inspector.text.inspect(value, label=key)
        if result.level_id != "S1" or result.redacted != value:
            raise SecurityDenied("模型传输元数据含敏感内容")
    try:
        data = request.content
        if len(data) > 262144:
            raise ValueError

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
        encode_payload(value)
        from .llm import normalize_llm_endpoint

        target = adapter.targets.get((normalize_llm_endpoint(str(request.url)), value.get("model")))
        if target is None or target.target_id not in target_ids:
            raise SecurityDenied("当前客户端认证配置不得用于此实际模型目标")
        return value
    except SecurityDenied:
        raise
    except Exception:
        raise SecurityDenied("模型请求不是有界且无重复键的 JSON") from None


def create_http_client(adapter, *, backend=None, auth_headers=None, target_ids=None, **options):
    if (
        not isinstance(target_ids, (set, frozenset))
        or not target_ids
        or not target_ids.issubset({t.target_id for t in adapter.targets.values()})
    ):
        raise SecurityDenied("必须显式绑定客户端认证可使用的模型目标集合")
    target_ids = frozenset(target_ids)
    if not isinstance(auth_headers, dict) or any(
        not has_exact_type(key, str)
        or key.lower() not in _CREDENTIAL_HEADERS
        or not has_exact_type(value, str)
        for key, value in auth_headers.items()
    ):
        raise SecurityDenied("必须显式绑定可信模型认证头；无认证端点使用空配置")
    secret = secrets.token_bytes(32)
    credentials = {
        key.lower(): hmac.new(secret, value.encode(), hashlib.sha256).digest()
        for key, value in auth_headers.items()
    }
    if backend is None:
        try:
            import openai

            backend = "httpx2" if hasattr(openai, "DefaultHttpx2Client") else "httpx"
        except ImportError:
            backend = "httpx"
    if backend not in {"httpx", "httpx2"}:
        raise SecurityDenied("HTTP transport 后端未覆盖")
    httpx = importlib.import_module(backend)

    # 禁止 HTTPX 用 proxy/mount 自动选择另一条绕过本门禁的 transport。
    if set(options) - {"timeout", "verify", "cert", "headers", "follow_redirects", "event_hooks"}:
        raise SecurityDenied("HTTP 客户端选项可能绕过模型 transport")
    network = {key: options.pop(key) for key in ("verify", "cert") if key in options}

    def snapshot_request(request):
        return (
            str(request.url),
            tuple(request.headers.multi_items()),
            copy.deepcopy(request.extensions),
        )

    def selected_request(snapshot, payload):
        url, original_headers, extensions = snapshot
        headers = [
            (key, value)
            for key, value in original_headers
            if key.lower() not in {"content-length", "transfer-encoding"}
        ]
        return httpx.Request(
            "POST",
            url,
            headers=headers,
            content=encode_payload(payload).encode("utf-8"),
            extensions=extensions,
        )

    class GuardedAsyncTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport(retries=0, **network)

        async def handle_async_request(self, request):
            try:
                body = _decode(request, adapter, credentials, secret, target_ids)
                snapshot = snapshot_request(request)
            except SecurityDenied:
                adapter.current_call().stopped = True
                raise
            async with adapter.arequest(snapshot[0], body) as selected:
                approved = selected_request(snapshot, selected)
                operation = adapter.wire_operation()
                response = await self.inner.handle_async_request(approved)
            if adapter.results is None:
                return response
            try:
                from .llm_response import (
                    ResponseBuffer,
                    protect_completion,
                    response_headers,
                    safe_error,
                )

                if not 200 <= response.status_code < 300:
                    await response.aclose()
                    return httpx.Response(
                        response.status_code,
                        headers={
                            "content-type": "application/json",
                            **{
                                k: v
                                for k, v in response_headers(response).items()
                                if k == "retry-after"
                            },
                        },
                        content=safe_error(),
                    )
                headers = response_headers(response)
                buffer = ResponseBuffer(response.headers.get("content-encoding", "identity"))
                try:
                    async for chunk in response.stream:
                        buffer.add(chunk)
                finally:
                    await response.aclose()
                protected = await asyncio.to_thread(
                    protect_completion,
                    adapter,
                    operation,
                    buffer.finish(),
                    headers["content-type"] == "text/event-stream",
                )
                return httpx.Response(response.status_code, headers=headers, content=protected)
            except BaseException as error:
                adapter.current_call().stopped = True
                await response.aclose()
                if not isinstance(error, Exception):
                    raise
                raise SecurityDenied("模型结果未通过释放检查") from None

        async def aclose(self):
            await self.inner.aclose()

    return httpx.AsyncClient(transport=GuardedAsyncTransport(), trust_env=False, **options)
