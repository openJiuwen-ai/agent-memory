"""本地接入示例；固定开发身份、虚构数据及临时存储，不能用作生产认证。"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from jiuwen_memory.api import (
    Scope,
    assemble_server_runtime,
    build_dev_authenticator,
    internal_context,
)
from jiuwen_memory_adapter.agent_security import (
    Actor,
    ApprovalChoice,
    EgressPolicy,
    Hook,
    LlmTarget,
    SecurityPlugin,
    TargetRule,
)

console = logging.getLogger(__name__)


def sdk_settings(directory):
    return {
        "globals": {"privacy_enabled": True, "vector_enabled": False},
        "audit": {"default": "in_memory"},
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
                                "params": {"db_path": str(directory / "private.sqlite")},
                            }
                        },
                    },
                },
            }
        },
        "cryptography": {"private": {"target": "local", "params": {"key_provider": "private"}}},
        "key_provider": {
            "private": {"target": "local", "params": {"key_hex": secrets.token_hex(32)}}
        },
    }


def known_actor(actor):
    return (
        actor.tenant_id == "local"
        and actor.user_id == "developer"
        and actor.agent_id in {"main", "worker"}
    )


async def review(plugin, owner, wake):
    """终端是本地宿主控制面；审批不交给模型，也不解释聊天文字。"""
    while True:
        await wake.wait()
        wake.clear()
        for request in plugin.pending(owner):
            console.info(
                "安全确认：%s / %s → %s", request["level"], request["action"], request["target"]
            )
            options = request["options"]
            for index, option in enumerate(options, 1):
                console.info("%s: %s", index, option["label"])
            # 使用当前真实候选列表，不能假设三种选择始终都可用。
            while True:
                raw = await asyncio.to_thread(input, "选择序号：")
                if raw.isdecimal() and 1 <= int(raw) <= len(options):
                    break
            plugin.decide(
                actor=owner,
                approval_id=request["approval_id"],
                version=request["version"],
                choice=ApprovalChoice(options[int(raw) - 1]["value"]),
            )


async def run(args):
    from openjiuwen.core.runner import Runner

    key = os.environ["MINDVAULT_DEMO_API_KEY"]
    scope = Scope(org="local", user="developer")
    authenticator = build_dev_authenticator()  # 固定开发身份，仅限本地隔离示例。
    loop, wake = asyncio.get_running_loop(), asyncio.Event()
    targets = {"demo-model", "local-report", "current-user", "agent:main", "agent:worker"}
    allowed = {
        ("demo-model", "llm.chat.completions"),
        ("local-report", "send_report"),
        ("current-user", "agent.response"),
        ("agent:main", "agent.message"),
        ("agent:worker", "agent.message"),
    }
    producers = {
        "demo-model",
        "framework-model-result",
        "send_report",
        "agent:worker",
        "delegate_worker",
        "jiuwen_memory_get",
        "jiuwen_memory_list",
        "jiuwen_memory_search",
    }
    reports = []
    with TemporaryDirectory(prefix="jiuwen-security-demo-") as directory:
        runtime = await asyncio.to_thread(
            assemble_server_runtime, config=sdk_settings(Path(directory))
        )
        plugin, reviewer, running = None, None, None
        try:

            def security():
                return internal_context(authenticator)

            units = await asyncio.to_thread(
                runtime.api.add, "虚构联系人电话 13800138000", scope, security=security()
            )
            unit = units[0]
            plugin = SecurityPlugin(
                {"framework": "openjiuwen", "memory_backend": "jiuwen"},
                policy=EgressPolicy([TargetRule(target) for target in sorted(targets)]),
                targets=[LlmTarget("demo-model", args.provider, args.model, args.endpoint)],
                authorize=lambda operation: (
                    known_actor(operation.actor) and (operation.target, operation.action) in allowed
                ),
                authorize_input=lambda actor, hook, producer: (
                    known_actor(actor)
                    and hook
                    in {Hook.AFTER_LLM_CALL, Hook.AFTER_TOOL_CALL, Hook.BEFORE_AGENT_MESSAGE}
                    and producer in producers
                ),
                authorize_transfer=lambda sender, recipient, snapshot: (
                    known_actor(sender)
                    and known_actor(recipient)
                    and sender.user_id == recipient.user_id
                ),
                notify=lambda owner, event: loop.call_soon_threadsafe(wake.set),
            )
            owner = plugin.new_session(Actor("local", "developer", "demo", "main", "demo"))
            backend = plugin.connect_memory_backend(
                api=runtime.api,
                provider="demo-sdk",
                security_for=lambda actor: security(),
                scope_for=lambda actor: scope,
                authorize_use=lambda actor, reference, purpose: (
                    known_actor(actor)
                    and reference.scope[:1] == ("local",)
                    and purpose == "agent-memory-use"
                ),
                # 此声明只适用于上面显式启用的脱敏 SDK 配置，不能照抄到未知后端。
                protected_source_levels_for=lambda actor: frozenset({"S2", "S3"}),
            )
            # 先由宿主读取安全视图并建立来源；真实 unit ID 不发送给模型。
            await asyncio.to_thread(
                backend.invoke, "jiuwen_memory_get", {"unit_id": unit.id}, owner
            )

            def send_report(approved, context):
                reports.append(dict(approved))  # 演示终点仅为本进程内存。
                return {"body": "report recorded locally"}

            tool = plugin.tool(
                target="local-report",
                name="send_report",
                description="将 body 报告记录到本地内存，不访问网络。",
                input_schema={
                    "type": "object",
                    "properties": {"body": {"type": "string"}},
                    "required": ["body"],
                    "additionalProperties": False,
                },
                send=send_report,
            )
            await Runner.start()
            model = plugin.model("demo-model", api_key=key, max_retries=0)
            child = await plugin.agent(agent_id="worker", model=model)
            delegate = plugin.delegate_tool(child, name="delegate_worker")
            agent = await plugin.agent(
                agent_id="main", model=model, tools=[*plugin.memory_tools(backend), tool, delegate]
            )
            reviewer = asyncio.create_task(review(plugin, owner, wake))
            running = asyncio.create_task(agent.ainvoke(args.prompt, session=owner))
            done, _ = await asyncio.wait({reviewer, running}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # 终端 EOF/审批失败也应停止运行，不自动批准。
            console.info("%s", json.dumps(running.result(), ensure_ascii=False))
            console.info("本地工具执行次数：%s", len(reports))
        finally:
            for task in (running, reviewer):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (running, reviewer) if t), return_exceptions=True)
            try:
                if plugin is not None:
                    await plugin.aclose()
            finally:
                try:
                    await Runner.stop()
                finally:
                    await asyncio.to_thread(runtime.close)


def main():
    # 独立演示终端使用自己的日志输出，不改变宿主或框架的日志配置。
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    console.addHandler(handler)
    console.setLevel(logging.INFO)
    console.propagate = False
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="完整 Chat Completions URL")
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="demo-provider")
    parser.add_argument(
        "--prompt",
        default=(
            "请用 jiuwen_memory_list 读取示例记忆，调用 send_report，再委派 delegate_worker 总结。"
        ),
    )
    args = parser.parse_args()
    if not os.environ.get("MINDVAULT_DEMO_API_KEY"):
        parser.error("请设置 MINDVAULT_DEMO_API_KEY；不要把凭据放在聊天或命令行参数里")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
