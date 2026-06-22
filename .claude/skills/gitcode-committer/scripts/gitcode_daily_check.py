#!/usr/bin/env python3
# coding: utf-8
"""
定时任务脚本：每天检查 GitCode @ 通知并发送到飞书群。

用法：
    python gitcode_daily_check.py [--chat-id CHAT_ID]

环境变量：
    GITCODE_TOKEN - GitCode API Token
    FEISHU_WEBHOOK_URL - 飞书机器人 webhook（可选，用于发送消息）
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

from pr_client import PRClient
from pr_notifs import get_notifications, filter_bot_notifications


def format_feishu_message(
    notifications: list,
    owner: str,
    repo: str,
) -> str:
    """格式化飞书消息。

    Args:
        notifications: 通知列表。
        owner: 仓库 owner。
        repo: 仓库名。

    Returns:
        格式化的消息文本。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not notifications:
        return f"""📋 **GitCode 每日检查** ({now})

仓库: {owner}/{repo}

✅ 今日没有需要处理的 PR 通知

---
由 CI 自动发送"""

    # 按 PR 分组统计
    pr_count = {}
    for n in notifications:
        pr_num = n.get("html_url", "").split("/")[-1].split("?")[0]
        if pr_num:
            pr_count[pr_num] = pr_count.get(pr_num, 0) + 1

    lines = [
        f"📋 **GitCode 每日检查** ({now})",
        "",
        f"仓库: {owner}/{repo}",
        "",
        f"🔔 发现 **{len(pr_count)}** 个 PR 有 **{len(notifications)}** 条未读通知",
        "",
    ]

    # 列出 PR
    lines.append("| PR | 未读通知 |")
    lines.append("|:---:|:---:|")
    for pr_num in sorted(pr_count.keys(), key=lambda x: -int(x) if x.isdigit() else 0)[:10]:
        count = pr_count[pr_num]
        pr_url = f"https://gitcode.com/{owner}/{repo}/pull/{pr_num}"
        lines.append(f"| [#{pr_num}]({pr_url}) | {count} |")

    if len(pr_count) > 10:
        lines.append(f"| ... | 还有 {len(pr_count) - 10} 个 |")

    lines.append("")
    lines.append("---")
    lines.append("💡 使用 `gitcode-review <N>` 查看详情")
    lines.append("由 CI 自动发送")

    return "\n".join(lines)


def send_to_feishu_webhook(webhook_url: str, message: str) -> bool:
    """通过 webhook 发送消息到飞书。

    Args:
        webhook_url: 飞书机器人 webhook URL。
        message: 消息内容。

    Returns:
        是否发送成功。
    """
    import urllib.request

    payload = json.dumps({
        "msg_type": "interactive",
        "card": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": message,
                }
            ]
        }
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("code") == 0
    except Exception as e:
        print(f"发送失败: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GitCode 每日检查")
    parser.add_argument(
        "--chat-id",
        help="飞书群 chat_id（用于发送消息）",
    )
    parser.add_argument(
        "--webhook",
        help="飞书机器人 webhook URL",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="检查最近 N 小时的通知（默认 24）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认用 .claude/skills/gitcode-config.json）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印消息，不发送",
    )

    args = parser.parse_args()

    # 获取通知
    client = PRClient.from_config(args.config)
    owner = client.upstream_owner
    repo = client.upstream_repo

    result = get_notifications(
        client, owner, repo,
        notif_type="referer",
    )

    notifications = result.get("list", [])
    filtered = filter_bot_notifications(notifications)

    # 格式化消息
    message = format_feishu_message(filtered, owner, repo)

    if args.dry_run:
        print(message)
        sys.exit(0)

    # 发送消息
    sent = False
    webhook_url = args.webhook or os.environ.get("FEISHU_WEBHOOK_URL", "")

    if webhook_url:
        sent = send_to_feishu_webhook(webhook_url, message)
        if sent:
            print("✅ 已发送到飞书群")
        else:
            print("❌ 发送失败")
    else:
        print("⚠️ 未配置飞书 webhook，只打印消息：")
        print(message)
