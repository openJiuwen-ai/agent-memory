#!/usr/bin/env python3
"""Small validated OpenAI-compatible cloud embedding proxy."""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


class State:
    def __init__(self, args: argparse.Namespace) -> None:
        self.upstream_url = args.upstream_url
        self.key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
        self.model = args.model
        self.dimension = args.dimension
        self.timeout = args.timeout
        self.audit = Path(args.audit_jsonl)
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        self.sem = threading.Semaphore(args.max_concurrency)
        self.lock = threading.Lock()
        self.metrics = {
            "requests": 0,
            "texts": 0,
            "successes": 0,
            "failures": 0,
            "upstream_attempts": 0,
            "upstream_429": 0,
            "upstream_81011": 0,
            "upstream_20015": 0,
        }

    def record(self, event: dict) -> None:
        payload = {"ts": time.time(), **event}
        with self.lock:
            with self.audit.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def bump(self, key: str, amount: int = 1) -> None:
        with self.lock:
            self.metrics[key] = self.metrics.get(key, 0) + amount

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.metrics)


class Handler(BaseHTTPRequestHandler):
    server_version = "CloudEmbeddingProxy/1.1"

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        return

    def send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "backend": "openai-compatible-cloud-embedding",
                    "model": self.state.model,
                    "dimension": self.state.dimension,
                },
            )
            return
        if self.path == "/metrics":
            self.send_json(200, self.state.snapshot())
            return
        if self.path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{"id": self.state.model}]})
            return
        self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/embeddings":
            self.send_json(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            values = payload.get("input")
            if isinstance(values, str):
                text_count = 1
            elif isinstance(values, list) and all(isinstance(item, str) for item in values):
                text_count = len(values)
            else:
                raise ValueError("input must be a string or list of strings")
        except Exception as exc:
            self.send_json(400, {"error": {"message": str(exc), "type": "invalid_request"}})
            return

        payload["model"] = self.state.model
        # OpenAI's Python SDK requests base64 embeddings by default.  The
        # SiliconFlow endpoint honors that hint, while Mem2.0 expects a list of
        # 1024 floats.  Omit the transport encoding hint so the upstream emits
        # its native float-array response; this changes no embedding values.
        payload.pop("encoding_format", None)
        self.state.bump("requests")
        self.state.bump("texts", text_count)
        started = time.perf_counter()
        last_status = 502
        last_body: dict = {"error": {"message": "upstream unavailable"}}
        backoffs = (0, 2, 10, 30, 60, 120)
        with self.state.sem:
            for attempt, delay in enumerate(backoffs, start=1):
                if delay:
                    time.sleep(delay)
                self.state.bump("upstream_attempts")
                try:
                    response = requests.post(
                        self.state.upstream_url,
                        headers={
                            "Authorization": f"Bearer {self.state.key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=self.state.timeout,
                    )
                    last_status = response.status_code
                    try:
                        last_body = response.json()
                    except Exception:
                        last_body = {"error": {"message": response.text[:500]}}
                    body_text = json.dumps(last_body, ensure_ascii=False)
                    is_81011 = "ModelArts.81011" in body_text
                    # SiliconFlow occasionally returns code 20015 for an
                    # otherwise valid input under load.  Replaying the exact
                    # request succeeds, so treat it as transport instability,
                    # never as a missing-vector fallback.
                    is_20015 = (
                        response.status_code == 400
                        and str(last_body.get("code", "")) == "20015"
                    )
                    if response.status_code == 429:
                        self.state.bump("upstream_429")
                    if is_81011:
                        self.state.bump("upstream_81011")
                    if is_20015:
                        self.state.bump("upstream_20015")
                    retryable = (
                        response.status_code in {429, 500, 502, 503, 504}
                        or is_81011
                        or is_20015
                    )
                    if response.status_code == 200 and not is_81011:
                        data = last_body.get("data") or []
                        vectors = [item.get("embedding") or [] for item in data]
                        valid = (
                            len(vectors) == text_count
                            and all(len(vector) == self.state.dimension for vector in vectors)
                            and all(
                                math.isfinite(float(value))
                                for vector in vectors
                                for value in vector
                            )
                        )
                        if not valid:
                            last_status = 502
                            last_body = {
                                "error": {
                                    "message": "invalid embedding cardinality/dimension/nonfinite",
                                    "type": "invalid_upstream_embedding",
                                }
                            }
                            retryable = True
                        else:
                            self.state.bump("successes")
                            self.state.record(
                                {
                                    "event": "embedding_success",
                                    "texts": text_count,
                                    "attempt": attempt,
                                    "elapsed_seconds": time.perf_counter() - started,
                                    "dimension": self.state.dimension,
                                }
                            )
                            self.send_json(200, last_body)
                            return
                    if not retryable:
                        break
                except Exception as exc:
                    last_status = 502
                    last_body = {"error": {"message": repr(exc), "type": "upstream_exception"}}

        self.state.bump("failures")
        self.state.record(
            {
                "event": "embedding_failure",
                "texts": text_count,
                "elapsed_seconds": time.perf_counter() - started,
                "status": last_status,
                "error": last_body.get("error"),
            }
        )
        self.send_json(last_status if 400 <= last_status <= 599 else 502, last_body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--model", default="bge-m3")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--audit-jsonl", required=True)
    args = parser.parse_args()
    state = State(args)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.state = state  # type: ignore[attr-defined]
    state.record({"event": "proxy_started", "port": args.port, "model": args.model})
    server.serve_forever()


if __name__ == "__main__":
    main()
