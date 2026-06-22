#!/usr/bin/env python3
# coding: utf-8
"""
GitCode PR 通知获取脚本。

通过 GitCode Notifications API 获取艾特当前用户的 PR。
API: GET /repos/:owner/:repo/notifications?type=referer
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

from pr_client import PRClient


def get_notifications(
    client: PRClient,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    notif_type: str = "referer",
    unread: Optional[bool] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    """获取仓库通知。

    Args:
        client: PRClient 实例。
        owner: 仓库 owner。
        repo: 仓库名。
        notif_type: 通知类型 (all/event/referer)。
        unread: 是否未读。
        since: 获取此时间后更新的通知 (ISO 8601)。
        before: 获取此时间前更新的通知 (ISO 8601)。
        page: 页码。
        per_page: 每页数量。

    Returns:
        通知列表响应。
    """
    owner = owner or client.upstream_owner
    repo = repo or client.upstream_repo

    path = f"/repos/{owner}/{repo}/notifications"
    params = {
        "type": notif_type,
        "page": page,
        "per_page": per_page,
    }
    if unread is not None:
        params["unread"] = str(unread).lower()
    if since:
        params["since"] = since
    if before:
        params["before"] = before

    return client._request("GET", path, params=params)


def extract_pr_info(notif: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从通知中提取 PR 信息。

    Args:
        notif: 通知对象。

    Returns:
        PR 信息字典，或 None。
    """
    html_url = notif.get("html_url", "")
    # 匹配 PR/MR 编号
    # https://gitcode.com/openJiuwen/deepsearch/merge_requests/848?...
    match = re.search(r"/(?:pull|merge_requests)/(\d+)", html_url)
    if not match:
        return None

    pr_number = int(match.group(1))
    content = notif.get("content", "")

    return {
        "number": pr_number,
        "html_url": html_url.split("?")[0],  # 去掉查询参数
        "content": content,
        "type": notif.get("type", ""),
        "unread": notif.get("unread", True),
        "update_at": notif.get("update_at", ""),
        "actor": notif.get("actor", {}).get("login", "unknown"),
    }


# 要过滤的 bot 用户列表
BOT_USERS = {"gitcode-bot", "renovate-bot"}


def filter_bot_notifications(
    notifications: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """过滤掉 bot 用户的通知。

    Args:
        notifications: 通知列表。

    Returns:
        过滤后的通知列表。
    """
    return [n for n in notifications if n.get("actor", {}).get("login", "") not in BOT_USERS]


def format_notification_report(
    notifications: List[Dict[str, Any]],
    owner: str,
    repo: str,
    show_unread_only: bool = False,
) -> str:
    """格式化通知报告。

    Args:
        notifications: 通知列表。
        owner: 仓库 owner。
        repo: 仓库名。
        show_unread_only: 是否只显示未读。

    Returns:
        格式化的报告字符串。
    """
    if not notifications:
        return f"📭 没有找到艾特你的通知 ({owner}/{repo})"

    # 按 PR 分组
    pr_groups: Dict[int, List[Dict[str, Any]]] = {}
    for notif in notifications:
        pr_info = extract_pr_info(notif)
        if not pr_info:
            continue
        pr_num = pr_info["number"]
        if pr_num not in pr_groups:
            pr_groups[pr_num] = []
        pr_groups[pr_num].append(pr_info)

    if not pr_groups:
        return f"📭 没有找到相关的 PR 通知 ({owner}/{repo})"

    lines = []
    lines.append(f"## 📣 艾特你的 PR ({owner}/{repo})")
    lines.append("")
    lines.append(f"找到 {len(pr_groups)} 个 PR，共 {len(notifications)} 条通知：")
    lines.append("")

    for pr_num in sorted(pr_groups.keys(), reverse=True):
        notifs = pr_groups[pr_num]
        latest = notifs[0]

        # 统计未读
        unread_count = sum(1 for n in notifs if n["unread"])
        status = "🆕" if unread_count > 0 else "✅"

        lines.append(f"### {status} #{pr_num}")
        lines.append(f"- 链接: {latest['html_url']}")
        lines.append(f"- 通知数: {len(notifs)} ({unread_count} 未读)")
        lines.append("")

        for n in notifs[:3]:  # 最多显示 3 条
            unread_mark = "**[未读]**" if n["unread"] else "[已读]"
            lines.append(f"  {unread_mark} @{n['actor']}: {n['content'][:60]}...")

        if len(notifs) > 3:
            lines.append(f"  ... 还有 {len(notifs) - 3} 条通知")
        lines.append("")

    lines.append("---")
    lines.append(f"💡 使用 `gitcode-review <N>` 查看详情")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="获取艾特你的 GitCode PR 通知")
    parser.add_argument(
        "--type", "-t",
        choices=["all", "event", "referer"],
        default="referer",
        help="通知类型: all/event/referer (默认 referer，即 @通知)",
    )
    parser.add_argument(
        "--unread",
        action="store_true",
        default=None,
        help="只显示未读通知",
    )
    parser.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help="显示所有通知（包括已读）",
    )
    parser.add_argument(
        "--hours", "-H",
        type=int,
        default=None,
        help="只显示最近 N 小时的通知",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认用 .claude/skills/gitcode-config.json）",
    )
    parser.add_argument(
        "--owner",
        help="仓库 owner",
    )
    parser.add_argument(
        "--repo",
        help="仓库名",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 格式",
    )

    args = parser.parse_args()

    client = PRClient.from_config(args.config)

    owner = args.owner or client.upstream_owner
    repo = args.repo or client.upstream_repo

    # 计算时间过滤
    since = None
    if args.hours:
        dt = datetime.now() - timedelta(hours=args.hours)
        since = dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    # 确定是否只显示未读
    unread = None if args.show_all else True

    result = get_notifications(
        client, owner, repo,
        notif_type=args.type,
        unread=unread,
        since=since,
    )

    notifications = result.get("list", [])
    total = result.get("total", 0)

    # 过滤 bot 通知
    filtered = filter_bot_notifications(notifications)

    if args.json:
        print(json.dumps({"total": len(filtered), "list": filtered}, indent=2, ensure_ascii=False))
    else:
        if not filtered:
            print(f"📭 没有需要处理的 PR 通知（已过滤 {len(notifications)} 条 bot 通知）")
        else:
            print(format_notification_report(filtered, owner, repo))
            if len(notifications) > len(filtered):
                print(f"\n📊 已过滤 {len(notifications) - len(filtered)} 条 bot 通知")
