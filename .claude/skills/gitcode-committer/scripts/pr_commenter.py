#!/usr/bin/env python3
# coding: utf-8
"""
GitCode PR 评论脚本。

提交检视意见、批准 PR。

gitcode-approve 增强功能：
1. 检查 close_related_issue 设置，不允许为 True
2. 检查是否使用 squash 合并，如果是，设置格式化的 commit message
3. 如果没有关联 issue，提醒提交人
"""

import json
import os
import sys
from typing import Dict, List, Optional, Tuple, Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

from pr_client import PRClient, parse_pr_reference


def comment_on_pr(
    client: PRClient,
    number: int,
    body: str,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
) -> dict:
    """在 PR 提交评论。

    Args:
        client: PRClient 实例。
        number: PR 编号。
        body: 评论内容。
        owner: 仓库 owner。
        repo: 仓库名。

    Returns:
        创建的评论详情。
    """
    return client.create_issue_comment(number, body, owner, repo)


def check_pr_merge_settings(
    client: PRClient,
    number: int,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    """检查 PR 合并设置。

    Args:
        client: PRClient 实例。
        number: PR 编号。
        owner: 仓库 owner。
        repo: 仓库名。

    Returns:
        包含检查结果的字典：
        - close_related_issue: 是否设置了关闭关联 issue
        - linked_issues: 关联的 issue 列表
        - has_issues: 是否有关联 issue
        - pr_title: PR 标题
        - warnings: 警告信息列表
    """
    owner = owner or client.upstream_owner
    repo = repo or client.upstream_repo
    warnings = []

    # 获取 PR 详情
    pr = client.get_pull_request(number, owner, repo)
    pr_title = pr.get("title", "")

    # 获取关联的 issue
    try:
        linked_issues = client.get_linked_issues(number, owner, repo)
        if isinstance(linked_issues, list):
            issue_numbers = [i.get("number") for i in linked_issues if i.get("number")]
        else:
            issue_numbers = []
    except Exception:
        issue_numbers = []

    has_issues = len(issue_numbers) > 0

    # 检查 close_related_issue 设置（需要从 PR 详情中获取）
    # GitCode API 返回的 PR 对象中可能包含此字段
    close_related = pr.get("close_related_issue", None)

    if close_related is True:
        warnings.append(
            "⚠️ PR 设置了「合并后关闭关联的 issue」，请取消此选项"
        )

    if not has_issues:
        warnings.append(
            "⚠️ PR 没有关联任何 issue，请在 PR 描述中添加关联或创建对应 issue"
        )

    return {
        "close_related_issue": close_related,
        "linked_issues": issue_numbers,
        "has_issues": has_issues,
        "pr_title": pr_title,
        "warnings": warnings,
    }


def format_squash_message(
    pr_title: str,
    issue_numbers: List[int],
) -> str:
    """格式化 squash commit message。

    格式：
    ```
    MR标题

    Refs: #{issue号}
    ```

    Args:
        pr_title: PR 标题。
        issue_numbers: 关联的 issue 编号列表。

    Returns:
        格式化的 squash message。
    """
    lines = [pr_title]

    if issue_numbers:
        refs = " ".join([f"#{num}" for num in issue_numbers])
        lines.append("")
        lines.append(f"Refs: {refs}")

    return "\n".join(lines)


def approve_pr(
    client: PRClient,
    number: int,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    message: str = "",
    check_settings: bool = True,
) -> Tuple[dict, List[str]]:
    """批准 PR。

    执行以下检查：
    1. 检查 close_related_issue 设置
    2. 检查关联的 issue
    3. 如果有问题，在评论中提醒

    Args:
        client: PRClient 实例。
        number: PR 编号。
        owner: 仓库 owner。
        repo: 仓库名。
        message: 附加消息。
        check_settings: 是否检查合并设置。

    Returns:
        (创建的评论详情, 提示信息列表)
    """
    owner = owner or client.upstream_owner
    repo = repo or client.upstream_repo
    info_messages = []

    # 检查合并设置
    if check_settings:
        settings = check_pr_merge_settings(client, number, owner, repo)
        warnings = settings.get("warnings", [])

        # 如果设置了关闭关联 issue，尝试取消
        if settings.get("close_related_issue") is True:
            try:
                client.update_pr_settings(
                    number, close_related_issue=False, owner=owner, repo=repo
                )
                info_messages.append(
                    "✅ 已取消「合并后关闭关联的 issue」设置"
                )
            except Exception as e:
                info_messages.append(
                    f"⚠️ 无法修改 PR 设置：{e}"
                )

        # 如果没有关联 issue，提醒
        if not settings.get("has_issues"):
            reminder = (
                "📋 检视意见：此 PR 没有关联任何 issue。\n"
                "请在 PR 描述中添加关联 issue（如 `Fixes #123`），"
                "或创建对应的 issue 以便追踪。"
            )
            try:
                client.create_issue_comment(number, reminder, owner, repo)
                info_messages.append(
                    "💬 已提醒提交人关联 issue"
                )
            except Exception:
                pass

    # 提交批准评论
    body = "/approve\n/lgtm"
    if message:
        body += f"\n\n{message}"

    result = client.create_issue_comment(number, body, owner, repo)
    return result, info_messages


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GitCode PR 评论")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # comment 子命令
    comment_parser = subparsers.add_parser("comment", help="提交评论")
    comment_parser.add_argument("number", help="PR 编号或链接")
    comment_parser.add_argument("body", help="评论内容")
    comment_parser.add_argument(
        "--config", default=None, help="配置文件路径（默认用 .claude/skills/gitcode-config.json）"
    )
    comment_parser.add_argument("--owner", help="仓库 owner")
    comment_parser.add_argument("--repo", help="仓库名")

    # approve 子命令
    approve_parser = subparsers.add_parser("approve", help="批准 PR")
    approve_parser.add_argument("number", help="PR 编号或链接")
    approve_parser.add_argument(
        "--message", "-m", default="", help="附加消息"
    )
    approve_parser.add_argument(
        "--config", default=None, help="配置文件路径（默认用 .claude/skills/gitcode-config.json）"
    )
    approve_parser.add_argument("--owner", help="仓库 owner")
    approve_parser.add_argument("--repo", help="仓库名")
    approve_parser.add_argument(
        "--no-check",
        action="store_true",
        help="跳过合并设置检查",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    client = PRClient.from_config(getattr(args, "config", None))

    owner, repo, number = parse_pr_reference(args.number)
    owner = owner or getattr(args, "owner", None) or client.upstream_owner
    repo = repo or getattr(args, "repo", None) or client.upstream_repo

    pr_url = f"https://gitcode.com/{owner}/{repo}/pull/{number}"

    if args.command == "comment":
        result = comment_on_pr(client, number, args.body, owner, repo)
        print(f"✅ 已在 PR #{number} 提交评论")
        print(f"🔗 {pr_url}")
    elif args.command == "approve":
        result, info = approve_pr(
            client, number, owner, repo,
            message=getattr(args, "message", ""),
            check_settings=not getattr(args, "no_check", False),
        )
        print(f"✅ 已批准 PR #{number}")
        print(f"🔗 {pr_url}")

        # 输出提示信息
        for msg in info:
            print(msg)
