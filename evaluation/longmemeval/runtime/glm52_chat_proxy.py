#!/usr/bin/env python3
"""OpenAI-compatible chat proxy with content-free benchmark counters."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import tiktoken

try:  # Linux（SSH 原运行环境）
    import fcntl
except ImportError:  # Windows 本地冒烟
    fcntl = None

try:  # Windows 与 fcntl 等价的单字节非阻塞文件锁
    import msvcrt
except ImportError:
    msvcrt = None


def _try_lock(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    if msvcrt is None:
        raise RuntimeError("当前平台不支持文件锁")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    handle.seek(0)
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _global_file_semaphore(directory: str, slots: int):
    """Coordinate the upstream concurrency budget across proxy processes."""

    if not directory or slots <= 0:
        yield
        return
    lock_dir = Path(directory)
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = None
    while handle is None:
        for index in range(slots):
            candidate = open(
                lock_dir / f"slot-{index}.lock", "a+", encoding="utf-8"
            )
            if not _try_lock(candidate):
                candidate.close()
                continue
            handle = candidate
            break
        if handle is None:
            time.sleep(0.05)
    try:
        yield
    finally:
        _unlock(handle)
        handle.close()


def _counter() -> dict[str, float | int]:
    return {
        "requests": 0,
        "seconds": 0.0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "errors": 0,
    }


def _category(payload: dict[str, Any]) -> str:
    text = "\n".join(
        str(message.get("content", "")) for message in payload.get("messages") or []
    ).lower()
    if "extract facts, events, preferences, and context" in text:
        return "extract"
    if "generate layered disclosure views" in text:
        return "layer_annotate"
    if "dedup" in text or "duplicate" in text and "memory" in text:
        return "dedup"
    if "memory content merger" in text:
        return "merge"
    if "you are an answer judge" in text:
        return "judge"
    if "you are an ai research assistant" in text:
        return "answer"
    return "other"


class State:
    def __init__(
        self,
        max_total_tokens: int = 0,
        max_total_requests: int = 0,
        audit_path: str = "",
    ) -> None:
        self._lock = threading.Lock()
        self.max_total_tokens = max(0, max_total_tokens)
        self.max_total_requests = max(0, max_total_requests)
        self.audit_path = Path(audit_path) if audit_path else None
        self.reserved_tokens = 0
        self.reserved_requests = 0
        self.sequence = 0
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.started_at = time.time()
            self.total = _counter()
            self.categories: dict[str, dict[str, float | int]] = defaultdict(_counter)
            self.reserved_tokens = 0
            self.reserved_requests = 0
            self.transient_retries = 0
            self.transient_exhausted = 0
            self.budget_denials = 0
            self.permanent_upstream_errors = 0
            self.coalesced_requests = 0

    def note_operational_event(self, event: str) -> None:
        with self._lock:
            if event == "transient_retry":
                self.transient_retries += 1
            elif event == "transient_exhausted":
                self.transient_exhausted += 1
            elif event == "budget_denial":
                self.budget_denials += 1
            elif event == "permanent_upstream_error":
                self.permanent_upstream_errors += 1
            elif event == "coalesced_request":
                self.coalesced_requests += 1
            else:
                raise ValueError(f"unknown operational event: {event}")

    def try_reserve(
        self, prompt_tokens: int, completion_tokens: int
    ) -> tuple[bool, dict[str, int]]:
        requested = max(0, prompt_tokens) + max(0, completion_tokens)
        with self._lock:
            consumed = int(self.total["prompt_tokens"]) + int(self.total["completion_tokens"])
            remaining = max(0, self.max_total_tokens - consumed - self.reserved_tokens)
            consumed_requests = int(self.total["requests"])
            remaining_requests = max(
                0,
                self.max_total_requests
                - consumed_requests
                - self.reserved_requests,
            )
            token_admitted = self.max_total_tokens <= 0 or requested <= remaining
            request_admitted = (
                self.max_total_requests <= 0 or remaining_requests >= 1
            )
            admitted = token_admitted and request_admitted
            if admitted:
                self.reserved_tokens += requested
                self.reserved_requests += 1
            return admitted, {
                "limit": self.max_total_tokens,
                "consumed": consumed,
                "reserved": self.reserved_tokens,
                "requested": requested,
                "remaining": remaining,
                "request_limit": self.max_total_requests,
                "requests_consumed": consumed_requests,
                "requests_reserved": self.reserved_requests,
                "requests_remaining": remaining_requests,
                "token_admitted": int(token_admitted),
                "request_admitted": int(request_admitted),
            }

    def release_reservation(self, prompt_tokens: int, completion_tokens: int) -> None:
        requested = max(0, prompt_tokens) + max(0, completion_tokens)
        with self._lock:
            self.reserved_tokens = max(0, self.reserved_tokens - requested)
            self.reserved_requests = max(0, self.reserved_requests - 1)

    def audit(self, payload: dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        with self._lock:
            self.sequence += 1
            row = {"sequence": self.sequence, "timestamp": time.time(), **payload}
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def record(
        self,
        category: str,
        seconds: float,
        *,
        usage: dict[str, Any] | None = None,
        error: bool = False,
    ) -> None:
        usage = usage or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = int(
            completion_details.get("reasoning_tokens")
            or usage.get("reasoning_tokens")
            or 0
        )
        with self._lock:
            for item in (self.total, self.categories[category]):
                item["requests"] += 1
                item["seconds"] += seconds
                item["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                item["cached_tokens"] += int(
                    prompt_details.get("cached_tokens")
                    or usage.get("cached_tokens")
                    or 0
                )
                item["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                item["reasoning_tokens"] += reasoning_tokens
                item["errors"] += int(error)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "window_started_at": self.started_at,
                "window_seconds": time.time() - self.started_at,
                "total": json.loads(json.dumps(self.total)),
                "categories": json.loads(json.dumps(self.categories)),
                "token_budget": {
                    "limit": self.max_total_tokens,
                    "consumed": int(self.total["prompt_tokens"])
                    + int(self.total["completion_tokens"]),
                    "reserved": self.reserved_tokens,
                },
                "request_budget": {
                    "limit": self.max_total_requests,
                    "consumed": int(self.total["requests"]),
                    "reserved": self.reserved_requests,
                },
                "operational": {
                    "transient_retries": self.transient_retries,
                    "transient_exhausted": self.transient_exhausted,
                    "budget_denials": self.budget_denials,
                    "permanent_upstream_errors": self.permanent_upstream_errors,
                    "coalesced_requests": self.coalesced_requests,
                },
            }


def _estimate_prompt_tokens(payload: dict[str, Any], model: str) -> int:
    """Conservatively estimate serialized chat input before the upstream call."""

    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # JSON serialization includes roles/tool payloads. Keep 10% headroom for the
    # provider's hidden chat-template tokens so the configured total is a hard
    # operational ceiling in practice rather than a post-hoc alarm.
    return math.ceil((len(encoding.encode(serialized)) + 32) * 1.10)


_TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _is_permanent_budget_response(status_code: int, body: str) -> bool:
    lowered = body.lower()
    return status_code == 429 and (
        "budget_exceeded" in lowered or "budget has been exceeded" in lowered
    )


def _is_transient_response(status_code: int, body: str) -> bool:
    return (
        status_code in _TRANSIENT_HTTP_STATUSES
        and not _is_permanent_budget_response(status_code, body)
    )


def _is_empty_completion(response: httpx.Response) -> bool:
    """Retry successful chat responses that contain no usable answer text."""

    if not 200 <= response.status_code < 300:
        return False
    try:
        choices = response.json().get("choices") or []
        message = choices[0].get("message") or {}
    except (ValueError, IndexError, AttributeError):
        return False
    return not str(message.get("content") or "").strip() and not message.get(
        "tool_calls"
    )


def _retry_delay(attempt: int, base_seconds: float, max_seconds: float) -> float:
    """Return bounded exponential backoff after a failed upstream attempt."""

    if base_seconds <= 0 or max_seconds <= 0:
        return 0.0
    return min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        upstream_url: str,
        upstream_keys: list[str],
        upstream_model: str,
        local_token: str,
        timeout: float,
        disable_thinking: bool,
        reasoning_effort: str,
        max_output_tokens: int,
        max_upstream_concurrency: int,
        global_concurrency_dir: str,
        global_max_upstream_concurrency: int,
        max_total_tokens: int,
        max_total_requests: int,
        audit_path: str,
        transient_retry_max_attempts: int,
        transient_retry_base_seconds: float,
        transient_retry_max_seconds: float,
        transient_retry_max_elapsed_seconds: float,
        response_cache_ttl_seconds: float,
    ) -> None:
        self.upstream_url = upstream_url.rstrip("/") + "/chat/completions"
        if not upstream_keys:
            raise ValueError("at least one upstream API key is required")
        self.upstream_keys = tuple(upstream_keys)
        self._key_lock = threading.Lock()
        self._exhausted_key_indexes: set[int] = set()
        self.upstream_model = upstream_model
        self.local_token = local_token
        self.timeout = timeout
        self.disable_thinking = disable_thinking
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.max_upstream_concurrency = max_upstream_concurrency
        self.global_concurrency_dir = global_concurrency_dir
        self.global_max_upstream_concurrency = global_max_upstream_concurrency
        self.transient_retry_max_attempts = max(1, transient_retry_max_attempts)
        self.transient_retry_base_seconds = max(0.0, transient_retry_base_seconds)
        self.transient_retry_max_seconds = max(0.0, transient_retry_max_seconds)
        self.transient_retry_max_elapsed_seconds = max(
            0.0, transient_retry_max_elapsed_seconds
        )
        self.response_cache_ttl_seconds = max(0.0, response_cache_ttl_seconds)
        self._response_cache_lock = threading.Lock()
        self._response_cache: dict[
            str, tuple[float, int, dict[str, str], bytes]
        ] = {}
        self.upstream_semaphore = (
            threading.BoundedSemaphore(max_upstream_concurrency)
            if max_upstream_concurrency > 0
            else None
        )
        self.state = State(
            max_total_tokens=max_total_tokens,
            max_total_requests=max_total_requests,
            audit_path=audit_path,
        )
        super().__init__(address, Handler)

    def next_upstream_key(self) -> tuple[int, str] | None:
        """Return the highest-priority non-exhausted key."""

        with self._key_lock:
            for index, key in enumerate(self.upstream_keys):
                if index in self._exhausted_key_indexes:
                    continue
                return index, key
        return None

    def mark_key_exhausted(self, index: int) -> None:
        with self._key_lock:
            self._exhausted_key_indexes.add(index)

    def key_pool_status(self) -> dict[str, int]:
        with self._key_lock:
            exhausted = len(self._exhausted_key_indexes)
        return {
            "total": len(self.upstream_keys),
            "active": len(self.upstream_keys) - exhausted,
            "exhausted": exhausted,
        }

    def get_cached_response(
        self, fingerprint: str
    ) -> tuple[int, dict[str, str], bytes] | None:
        """Return a recent exact response so client retries do not duplicate API work."""

        if self.response_cache_ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._response_cache_lock:
            expired = [
                key
                for key, (deadline, *_rest) in self._response_cache.items()
                if deadline <= now
            ]
            for key in expired:
                self._response_cache.pop(key, None)
            cached = self._response_cache.get(fingerprint)
            if cached is None:
                return None
            _deadline, status, headers, content = cached
            return status, dict(headers), content

    def cache_response(self, fingerprint: str, response: httpx.Response) -> None:
        if self.response_cache_ttl_seconds <= 0 or response.status_code >= 400:
            return
        headers = {
            "content-type": response.headers.get("content-type", "application/json")
        }
        with self._response_cache_lock:
            self._response_cache[fingerprint] = (
                time.monotonic() + self.response_cache_ttl_seconds,
                response.status_code,
                headers,
                response.content,
            )


class Handler(BaseHTTPRequestHandler):
    server: ProxyServer

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {self.server.local_token}"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._bytes(status, data, "application/json")

    def _bytes(self, status: int, data: bytes, content_type: str) -> bool:
        """Write a response, treating a vanished retrying client as non-fatal."""

        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(
                200,
                {
                    "status": "ok",
                    "upstream_model": self.server.upstream_model,
                    "disable_thinking": self.server.disable_thinking,
                    "reasoning_effort": self.server.reasoning_effort,
                    "max_output_tokens": self.server.max_output_tokens,
                    "max_upstream_concurrency": (
                        self.server.max_upstream_concurrency
                    ),
                    "global_concurrency_dir": (
                        self.server.global_concurrency_dir
                    ),
                    "global_max_upstream_concurrency": (
                        self.server.global_max_upstream_concurrency
                    ),
                    "max_total_tokens": self.server.state.max_total_tokens,
                    "max_total_requests": self.server.state.max_total_requests,
                    "upstream_key_pool": self.server.key_pool_status(),
                    "transient_retry_max_attempts": (
                        self.server.transient_retry_max_attempts
                    ),
                    "transient_retry_max_elapsed_seconds": (
                        self.server.transient_retry_max_elapsed_seconds
                    ),
                    "response_cache_ttl_seconds": (
                        self.server.response_cache_ttl_seconds
                    ),
                },
            )
        elif self.path == "/metrics" and self._authorized():
            self._json(200, self.server.state.snapshot())
        elif self.path == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": self.server.upstream_model, "object": "model"}],
                },
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/metrics/reset":
            self.server.state.reset()
            self._json(200, {"status": "reset"})
            return
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        category = _category(payload)
        payload["model"] = self.server.upstream_model
        if self.server.max_output_tokens > 0:
            requested = payload.get("max_tokens")
            payload["max_tokens"] = (
                min(int(requested), self.server.max_output_tokens)
                if requested
                else self.server.max_output_tokens
            )
        if self.server.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
            # GLM-5.2 also exposes the OpenAI-compatible reasoning-effort knob;
            # ``none`` is the documented no-thinking setting. Sending both is
            # intentional because some LiteLLM gateways strip provider fields.
            payload["reasoning_effort"] = "none"
            # The current self-hosted LiteLLM/vLLM route honors the chat-template
            # controls (verified by a reasoning_tokens=0 probe).
            payload["enable_thinking"] = False
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif self.server.reasoning_effort:
            payload["reasoning_effort"] = self.server.reasoning_effort
        fingerprint = self._fingerprint(payload)
        cached = self.server.get_cached_response(fingerprint)
        if cached is not None:
            status, headers, content = cached
            self.server.state.note_operational_event("coalesced_request")
            self.server.state.audit(
                {
                    "event": "coalesced_response",
                    "category": category,
                    "status": status,
                    "fingerprint": fingerprint,
                    "waited_for_inflight": False,
                }
            )
            self._bytes(
                status,
                content,
                headers.get("content-type", "application/json"),
            )
            return
        estimated_prompt_tokens = _estimate_prompt_tokens(
            payload, self.server.upstream_model
        )
        reserved_completion_tokens = int(payload.get("max_tokens") or 0)
        admitted, budget = self.server.state.try_reserve(
            estimated_prompt_tokens, reserved_completion_tokens
        )
        if not admitted:
            self.server.state.note_operational_event("budget_denial")
            error_code = (
                "local_request_budget_exceeded"
                if not budget.get("request_admitted")
                else "local_token_budget_exceeded"
            )
            self.server.state.audit(
                {
                    "category": category,
                    "status": 400,
                    "admitted": False,
                    "estimated_prompt_tokens": estimated_prompt_tokens,
                    "reserved_completion_tokens": reserved_completion_tokens,
                    "budget": budget,
                }
            )
            self._json(
                400,
                {
                    "error": {
                        "type": error_code,
                        "code": error_code,
                        "message": (
                            "Per-episode token or request budget would be exceeded; "
                            "this permanent local guard must not be retried."
                        ),
                        "budget": budget,
                    }
                },
            )
            return
        started = time.perf_counter()
        try:
            response: httpx.Response | None = None
            cached_replay: tuple[int, dict[str, str], bytes] | None = None
            upstream_attempts = 0
            last_exception: Exception | None = None
            transient_exhausted_noted = False
            while upstream_attempts < self.server.transient_retry_max_attempts:
                selected_key = self.server.next_upstream_key()
                if selected_key is None:
                    break
                upstream_key_index, upstream_key = selected_key
                attempt_timeout = self.server.timeout
                max_elapsed = self.server.transient_retry_max_elapsed_seconds
                if max_elapsed > 0:
                    remaining = max_elapsed - (time.perf_counter() - started)
                    if remaining <= 0:
                        self.server.state.note_operational_event("transient_exhausted")
                        transient_exhausted_noted = True
                        break
                    attempt_timeout = min(attempt_timeout, max(0.1, remaining))
                upstream_attempts += 1
                try:
                    semaphore = self.server.upstream_semaphore
                    if semaphore is None:
                        with _global_file_semaphore(
                            self.server.global_concurrency_dir,
                            self.server.global_max_upstream_concurrency,
                        ):
                            cached_replay = self.server.get_cached_response(fingerprint)
                            if cached_replay is None:
                                response = httpx.post(
                                    self.server.upstream_url,
                                    headers={
                                        "Authorization": f"Bearer {upstream_key}"
                                    },
                                    json=payload,
                                    timeout=attempt_timeout,
                                )
                                self.server.cache_response(fingerprint, response)
                    else:
                        with semaphore:
                            with _global_file_semaphore(
                                self.server.global_concurrency_dir,
                                self.server.global_max_upstream_concurrency,
                            ):
                                cached_replay = self.server.get_cached_response(fingerprint)
                                if cached_replay is None:
                                    response = httpx.post(
                                        self.server.upstream_url,
                                        headers={
                                            "Authorization": f"Bearer {upstream_key}"
                                        },
                                        json=payload,
                                        timeout=attempt_timeout,
                                    )
                                    self.server.cache_response(fingerprint, response)
                    last_exception = None
                except Exception as exc:  # noqa: BLE001
                    last_exception = exc
                    response = None

                if cached_replay is not None:
                    break

                if response is not None and _is_permanent_budget_response(
                    response.status_code, response.text
                ):
                    self.server.mark_key_exhausted(upstream_key_index)
                    self.server.state.audit(
                        {
                            "event": "upstream_key_budget_exhausted",
                            "category": category,
                            "upstream_key_index": upstream_key_index,
                            "upstream_key_pool": self.server.key_pool_status(),
                        }
                    )
                    if self.server.key_pool_status()["active"] > 0:
                        continue
                    break

                response_is_transient = response is not None and _is_transient_response(
                    response.status_code, response.text
                )
                response_is_empty = (
                    response is not None and _is_empty_completion(response)
                )
                exception_is_transient = response is None and last_exception is not None
                should_retry = (
                    response_is_transient
                    or response_is_empty
                    or exception_is_transient
                )
                if not should_retry:
                    break
                if upstream_attempts >= self.server.transient_retry_max_attempts:
                    self.server.state.note_operational_event("transient_exhausted")
                    transient_exhausted_noted = True
                    break

                delay = _retry_delay(
                    upstream_attempts,
                    self.server.transient_retry_base_seconds,
                    self.server.transient_retry_max_seconds,
                )
                if max_elapsed > 0 and (
                    time.perf_counter() - started + delay >= max_elapsed
                ):
                    if not transient_exhausted_noted:
                        self.server.state.note_operational_event("transient_exhausted")
                        transient_exhausted_noted = True
                    break
                self.server.state.note_operational_event("transient_retry")
                self.server.state.audit(
                    {
                        "event": "transient_retry",
                        "category": category,
                        "attempt": upstream_attempts,
                        "upstream_status": (
                            response.status_code if response is not None else None
                        ),
                        "error_type": (
                            type(last_exception).__name__ if last_exception else None
                        ),
                        "empty_completion": response_is_empty,
                        "delay_seconds": delay,
                        "estimated_prompt_tokens": estimated_prompt_tokens,
                    }
                )
                if delay:
                    time.sleep(delay)

            if cached_replay is not None:
                status, headers, content = cached_replay
                self.server.state.note_operational_event("coalesced_request")
                self.server.state.audit(
                    {
                        "event": "coalesced_response",
                        "category": category,
                        "status": status,
                        "fingerprint": fingerprint,
                        "waited_for_inflight": True,
                    }
                )
                self._bytes(
                    status,
                    content,
                    headers.get("content-type", "application/json"),
                )
                return

            if response is None:
                if last_exception is not None:
                    raise last_exception
                raise RuntimeError("upstream request ended without a response")

            usage: dict[str, Any] = {}
            try:
                usage = dict(response.json().get("usage") or {})
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
            permanent_upstream_budget = _is_permanent_budget_response(
                response.status_code, response.text
            )
            if permanent_upstream_budget:
                self.server.state.note_operational_event("permanent_upstream_error")
            downstream_status = 400 if permanent_upstream_budget else response.status_code
            self.server.state.record(
                category,
                time.perf_counter() - started,
                usage=usage,
                error=response.status_code >= 400,
            )
            self.server.state.audit(
                {
                    "category": category,
                    "status": downstream_status,
                    "upstream_status": response.status_code,
                    "upstream_attempts": upstream_attempts,
                    "transient_retries": max(0, upstream_attempts - 1),
                    "admitted": True,
                    "estimated_prompt_tokens": estimated_prompt_tokens,
                    "reserved_completion_tokens": reserved_completion_tokens,
                    "usage": usage,
                }
            )
            downstream_content = response.content
            if permanent_upstream_budget:
                downstream_content = json.dumps(
                    {
                        "error": {
                            "type": "upstream_budget_exceeded",
                            "code": "upstream_budget_exceeded",
                            "message": (
                                "The upstream key budget is exhausted; this "
                                "permanent error must not be retried."
                            ),
                        }
                    }
                ).encode("utf-8")
            delivered = self._bytes(
                downstream_status,
                downstream_content,
                response.headers.get("content-type", "application/json"),
            )
            if not delivered:
                self.server.state.audit(
                    {
                        "event": "downstream_disconnected_after_upstream_success",
                        "category": category,
                        "status": downstream_status,
                        "fingerprint": fingerprint,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            self.server.state.record(category, time.perf_counter() - started, error=True)
            self.server.state.audit(
                {
                    "category": category,
                    "status": 502,
                    "admitted": True,
                    "estimated_prompt_tokens": estimated_prompt_tokens,
                    "reserved_completion_tokens": reserved_completion_tokens,
                    "error_type": type(exc).__name__,
                }
            )
            self._json(502, {"error": {"type": type(exc).__name__, "message": str(exc)}})
        finally:
            self.server.state.release_reservation(
                estimated_prompt_tokens, reserved_completion_tokens
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--upstream-api-key-file", required=True)
    parser.add_argument("--upstream-model", default="GLM-5.2")
    parser.add_argument("--local-token", default="mem2-local")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument(
        "--reasoning-effort",
        default="",
        help="Explicit OpenAI-compatible reasoning effort when thinking is enabled",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=0,
        help="Clamp max_tokens sent upstream; 0 preserves the caller request",
    )
    parser.add_argument(
        "--max-upstream-concurrency",
        type=int,
        default=0,
        help="Maximum concurrent upstream requests; 0 leaves concurrency unrestricted.",
    )
    parser.add_argument(
        "--global-concurrency-dir",
        default="",
        help="Shared advisory-lock directory for multiple proxy processes",
    )
    parser.add_argument(
        "--global-max-upstream-concurrency",
        type=int,
        default=0,
        help="Cross-process upstream request limit; 0 disables the global gate",
    )
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        default=0,
        help="Hard cumulative prompt+completion token budget; 0 disables it",
    )
    parser.add_argument(
        "--max-total-requests",
        type=int,
        default=0,
        help="Hard per-window upstream request budget; 0 disables it",
    )
    parser.add_argument(
        "--audit-jsonl",
        default="",
        help="Optional content-free per-request usage audit JSONL",
    )
    parser.add_argument(
        "--transient-retry-max-attempts",
        type=int,
        default=1,
        help="Proxy-internal attempts for transient 408/429/5xx/transport errors",
    )
    parser.add_argument(
        "--transient-retry-base-seconds",
        type=float,
        default=2.0,
        help="Initial exponential backoff between transient attempts",
    )
    parser.add_argument(
        "--transient-retry-max-seconds",
        type=float,
        default=15.0,
        help="Maximum exponential backoff between transient attempts",
    )
    parser.add_argument(
        "--transient-retry-max-elapsed-seconds",
        type=float,
        default=0.0,
        help=(
            "Maximum wall time for all attempts of one logical request; "
            "0 disables the deadline"
        ),
    )
    parser.add_argument(
        "--response-cache-ttl-seconds",
        type=float,
        default=0.0,
        help=(
            "Cache successful exact responses to coalesce downstream client retries; "
            "0 disables caching"
        ),
    )
    args = parser.parse_args()

    keys = [
        line.strip()
        for line in Path(args.upstream_api_key_file)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    keys = list(dict.fromkeys(keys))
    if not keys:
        parser.error("upstream API key file is empty")
    server = ProxyServer(
        (args.host, args.port),
        upstream_url=args.upstream_base_url,
        upstream_keys=keys,
        upstream_model=args.upstream_model,
        local_token=args.local_token,
        timeout=args.timeout,
        disable_thinking=args.disable_thinking,
        reasoning_effort=args.reasoning_effort.strip(),
        max_output_tokens=max(0, args.max_output_tokens),
        max_upstream_concurrency=max(0, args.max_upstream_concurrency),
        global_concurrency_dir=args.global_concurrency_dir.strip(),
        global_max_upstream_concurrency=max(
            0, args.global_max_upstream_concurrency
        ),
        max_total_tokens=max(0, args.max_total_tokens),
        max_total_requests=max(0, args.max_total_requests),
        audit_path=args.audit_jsonl,
        transient_retry_max_attempts=max(1, args.transient_retry_max_attempts),
        transient_retry_base_seconds=max(0.0, args.transient_retry_base_seconds),
        transient_retry_max_seconds=max(0.0, args.transient_retry_max_seconds),
        transient_retry_max_elapsed_seconds=max(
            0.0, args.transient_retry_max_elapsed_seconds
        ),
        response_cache_ttl_seconds=max(0.0, args.response_cache_ttl_seconds),
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "host": args.host,
                "port": args.port,
                "upstream_model": args.upstream_model,
                "disable_thinking": args.disable_thinking,
                "reasoning_effort": args.reasoning_effort.strip(),
                "max_output_tokens": max(0, args.max_output_tokens),
                "max_upstream_concurrency": max(0, args.max_upstream_concurrency),
                "global_concurrency_dir": args.global_concurrency_dir.strip(),
                "global_max_upstream_concurrency": max(
                    0, args.global_max_upstream_concurrency
                ),
                "max_total_tokens": max(0, args.max_total_tokens),
                "max_total_requests": max(0, args.max_total_requests),
                "transient_retry_max_attempts": max(
                    1, args.transient_retry_max_attempts
                ),
                "transient_retry_max_elapsed_seconds": max(
                    0.0, args.transient_retry_max_elapsed_seconds
                ),
                "response_cache_ttl_seconds": max(
                    0.0, args.response_cache_ttl_seconds
                ),
                "audit_jsonl": args.audit_jsonl,
            }
        ),
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
