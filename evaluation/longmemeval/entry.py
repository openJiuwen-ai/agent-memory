"""启动 SSH 同款 GLM-5.2 双代理后运行 LongMemEval。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from evaluation.shared.env import EVALUATION_DIR, load_evaluation_env, require_env


def _argument_value(argv: list[str], option: str) -> str | None:
    try:
        return argv[argv.index(option) + 1]
    except (ValueError, IndexError):
        return None


def _wait_ready(process: subprocess.Popen, port: int, log_path: Path) -> None:
    deadline = time.monotonic() + 20.0
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"GLM 代理启动失败，日志: {log_path}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"等待 GLM 代理超时，日志: {log_path}")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _proxy_command(
    *,
    port: int,
    key_file: str,
    upstream_url: str,
    model: str,
    local_token: str,
    output_dir: Path,
    category: str,
    disable_thinking: bool,
) -> list[str]:
    concurrency = os.getenv("LONGMEMEVAL_CHAT_CONCURRENCY", "10")
    attempts = os.getenv("LONGMEMEVAL_PROXY_RETRY_ATTEMPTS", "4")
    command = [
        sys.executable,
        "-m",
        "evaluation.longmemeval.runtime.glm52_chat_proxy",
        "--port",
        str(port),
        "--upstream-base-url",
        upstream_url,
        "--upstream-api-key-file",
        key_file,
        "--upstream-model",
        model,
        "--local-token",
        local_token,
        "--timeout",
        os.getenv("LONGMEMEVAL_PROXY_TIMEOUT", "900"),
        "--max-upstream-concurrency",
        concurrency,
        "--transient-retry-max-attempts",
        attempts,
        "--transient-retry-base-seconds",
        "2",
        "--transient-retry-max-seconds",
        "15",
        "--audit-jsonl",
        str(output_dir / f"{category}_proxy_audit.jsonl"),
    ]
    if disable_thinking:
        command.append("--disable-thinking")
        command.extend(["--max-output-tokens", "4096"])
    else:
        command.extend(
            [
                "--max-output-tokens",
                os.getenv("LONGMEMEVAL_ANSWER_MAX_TOKENS", "16384"),
            ]
        )
    return command


def main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    load_evaluation_env()
    if "-h" in args or "--help" in args:
        from evaluation.longmemeval.run import main as run_main

        return run_main(args)

    required = require_env(
        "LONGMEMEVAL_CHAT_UPSTREAM_URL",
        "LONGMEMEVAL_CHAT_MODEL",
        "LONGMEMEVAL_CHAT_API_KEYS",
        "LONGMEMEVAL_EMBED_UPSTREAM_URL",
        "LONGMEMEVAL_EMBED_MODEL",
        "LONGMEMEVAL_EMBED_API_KEY",
    )
    output_raw = _argument_value(args, "--output-dir")
    if output_raw:
        output_dir = Path(output_raw).resolve()
    else:
        output_dir = (
            EVALUATION_DIR
            / "outputs"
            / "longmemeval"
            / datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
        )
        args.extend(["--output-dir", str(output_dir)])
    output_dir.mkdir(parents=True, exist_ok=True)

    extract_port = int(os.getenv("LONGMEMEVAL_EXTRACT_PROXY_PORT", "18938"))
    qa_port = int(os.getenv("LONGMEMEVAL_QA_PROXY_PORT", "18939"))
    embed_port = int(os.getenv("LONGMEMEVAL_EMBED_PROXY_PORT", "18937"))
    local_token = os.getenv("LONGMEMEVAL_LOCAL_TOKEN", "mem2-local")
    key_values = [
        value.strip()
        for value in required["LONGMEMEVAL_CHAT_API_KEYS"].split(",")
        if value.strip()
    ]
    if not key_values:
        raise RuntimeError("LONGMEMEVAL_CHAT_API_KEYS 没有可用密钥")

    secret_paths: list[str] = []
    processes: list[subprocess.Popen] = []
    log_stack = ExitStack()
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="mem2-longmemeval-", suffix=".keys", delete=False
        ) as key_file:
            key_file.write("\n".join(dict.fromkeys(key_values)) + "\n")
            key_path = key_file.name
            secret_paths.append(key_path)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="mem2-longmemeval-",
            suffix=".embed-key",
            delete=False,
        ) as embed_key_file:
            embed_key_file.write(required["LONGMEMEVAL_EMBED_API_KEY"] + "\n")
            embed_key_path = embed_key_file.name
            secret_paths.append(embed_key_path)

        embed_log_path = output_dir / "embed_proxy.log"
        embed_log = log_stack.enter_context(
            embed_log_path.open("w", encoding="utf-8")
        )
        embed_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "evaluation.longmemeval.runtime.cloud_bge_proxy",
                "--port",
                str(embed_port),
                "--upstream-url",
                required["LONGMEMEVAL_EMBED_UPSTREAM_URL"],
                "--api-key-file",
                embed_key_path,
                "--model",
                required["LONGMEMEVAL_EMBED_MODEL"],
                "--dimension",
                os.getenv("LONGMEMEVAL_EMBED_DIMENSION", "1024"),
                "--max-concurrency",
                os.getenv("LONGMEMEVAL_EMBED_CONCURRENCY", "4"),
                "--audit-jsonl",
                str(output_dir / "embed_proxy_audit.jsonl"),
            ],
            cwd=EVALUATION_DIR.parent,
            stdout=embed_log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        processes.append(embed_process)
        _wait_ready(embed_process, embed_port, embed_log_path)

        for category, port, disable_thinking in (
            ("extract", extract_port, True),
            ("answer_judge", qa_port, False),
        ):
            log_path = output_dir / f"{category}_proxy.log"
            log_handle = log_stack.enter_context(
                log_path.open("w", encoding="utf-8")
            )
            command = _proxy_command(
                port=port,
                key_file=key_path,
                upstream_url=required["LONGMEMEVAL_CHAT_UPSTREAM_URL"],
                model=required["LONGMEMEVAL_CHAT_MODEL"],
                local_token=local_token,
                output_dir=output_dir,
                category=category,
                disable_thinking=disable_thinking,
            )
            process = subprocess.Popen(
                command,
                cwd=EVALUATION_DIR.parent,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            processes.append(process)
            _wait_ready(process, port, log_path)

        os.environ["API_BASE"] = f"http://127.0.0.1:{extract_port}/v1"
        os.environ["MODEL_NAME"] = required["LONGMEMEVAL_CHAT_MODEL"]
        os.environ["API_KEY"] = local_token
        os.environ["JUDGE_BASE_URL"] = f"http://127.0.0.1:{qa_port}/v1"
        os.environ["JUDGE_MODEL"] = required["LONGMEMEVAL_CHAT_MODEL"]
        os.environ["JUDGE_API_KEY"] = local_token
        os.environ["EMBED_BASE_URL"] = f"http://127.0.0.1:{embed_port}/v1"
        os.environ["EMBED_API_KEY"] = local_token
        os.environ["EMBED_MODEL_NAME"] = required["LONGMEMEVAL_EMBED_MODEL"]
        os.environ["LME_CLIENT_API_AUDIT_PATH"] = str(
            output_dir / "client_api_audit.jsonl"
        )

        from evaluation.longmemeval.run import main as run_main

        return run_main(args)
    finally:
        for process in reversed(processes):
            _stop(process)
        log_stack.close()
        for secret_path in secret_paths:
            try:
                Path(secret_path).unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
