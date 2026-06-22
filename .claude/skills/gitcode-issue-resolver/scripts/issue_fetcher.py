#!/usr/bin/env python3
# coding: utf-8
"""
Issue 获取 CLI 工具。

从 GitCode upstream 仓库获取 Issue 详情或列表，
输出结构化 JSON 供 Claude 解析。

用法:
    # 获取单个 issue（含评论）
    python issue_fetcher.py --number 42

    # 列出 open issues
    python issue_fetcher.py --list --state open

    # 按标签过滤
    python issue_fetcher.py --list --labels bug

    # 按指派人过滤
    python issue_fetcher.py --list --assignee SnapeK

    # 指定配置文件
    python issue_fetcher.py --number 42 --config gitcode-config.json
"""

import argparse
import json
import os
import sys

# 支持从 scripts/ 目录或项目根目录运行
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.append(_SCRIPT_DIR)

from gitcode_client import GitCodeClient, GitCodeClientError


def _format_issue(issue: dict) -> dict:
    """提取 Issue 关键字段，输出精简结构。

    Args:
        issue: GitCode API 返回的原始 Issue 数据。

    Returns:
        精简后的 Issue 字典。
    """
    labels = []
    for label in issue.get("labels", []):
        if isinstance(label, dict):
            labels.append(label.get("name", ""))
        else:
            labels.append(str(label))

    assignee = issue.get("assignee")
    assignee_name = ""
    if isinstance(assignee, dict):
        assignee_name = assignee.get("login", "")

    return {
        "number": issue.get("number"),
        "title": issue.get("title", ""),
        "state": issue.get("state", ""),
        "labels": labels,
        "assignee": assignee_name,
        "body": issue.get("body", ""),
        "created_at": issue.get("created_at", ""),
        "updated_at": issue.get("updated_at", ""),
        "html_url": issue.get("html_url", ""),
    }


def _format_comment(comment: dict) -> dict:
    """提取评论关键字段。

    Args:
        comment: GitCode API 返回的原始评论数据。

    Returns:
        精简后的评论字典。
    """
    user = comment.get("user", {})
    return {
        "id": comment.get("id"),
        "author": user.get("login", "") if user else "",
        "body": comment.get("body", ""),
        "created_at": comment.get("created_at", ""),
    }


def fetch_issue(
    client: GitCodeClient,
    number: int,
) -> dict:
    """获取单个 Issue 详情（含评论）。

    Args:
        client: GitCode API 客户端。
        number: Issue 编号。

    Returns:
        包含 Issue 详情和评论的字典。
    """
    issue = client.get_issue(number)
    result = _format_issue(issue)

    comments_raw = client.get_issue_comments(number)
    result["comments"] = [
        _format_comment(c) for c in comments_raw
    ]
    return result


def list_issues(
    client: GitCodeClient,
    state: str = "open",
    labels: str = "",
    assignee: str = "",
    page: int = 1,
    per_page: int = 20,
) -> list:
    """获取 Issue 列表。

    Args:
        client: GitCode API 客户端。
        state: Issue 状态过滤。
        labels: 标签过滤。
        assignee: 指派人过滤。
        page: 页码。
        per_page: 每页数量。

    Returns:
        精简后的 Issue 列表。
    """
    issues = client.list_issues(
        state=state,
        labels=labels,
        assignee=assignee,
        page=page,
        per_page=per_page,
    )
    return [_format_issue(i) for i in issues]


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="GitCode Issue 获取工具",
    )
    parser.add_argument(
        "--number",
        type=int,
        help="获取指定编号的 Issue（含评论）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_mode",
        help="列出 Issue",
    )
    parser.add_argument(
        "--state",
        default="open",
        choices=["open", "closed", "all"],
        help="Issue 状态过滤（默认 open）",
    )
    parser.add_argument(
        "--labels",
        default="",
        help="按标签过滤（逗号分隔）",
    )
    parser.add_argument(
        "--assignee",
        default="",
        help="按指派人过滤",
    )
    parser.add_argument(
        "--page",
        type=int,
        default=1,
        help="页码（默认 1）",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=20,
        help="每页数量（默认 20）",
    )
    parser.add_argument(
        "--config",
        default="",
        help="配置文件路径",
    )
    return parser


def main() -> None:
    """CLI 入口。"""
    parser = _build_parser()
    args = parser.parse_args()

    if not args.number and not args.list_mode:
        parser.error("请指定 --number 或 --list")

    config_path = args.config
    if not config_path:
        # 尝试从项目根目录查找
        root = os.path.dirname(
            os.path.dirname(_SCRIPT_DIR)
        )
        candidate = os.path.join(
            root, "gitcode-config.json"
        )
        if os.path.exists(candidate):
            config_path = candidate

    try:
        client = GitCodeClient.from_config(config_path)
    except Exception as exc:
        print(
            json.dumps(
                {"error": f"初始化失败: {exc}"},
                ensure_ascii=False,
            )
        )
        sys.exit(1)

    try:
        if args.number:
            result = fetch_issue(client, args.number)
        else:
            result = list_issues(
                client,
                state=args.state,
                labels=args.labels,
                assignee=args.assignee,
                page=args.page,
                per_page=args.per_page,
            )
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )
    except GitCodeClientError as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "status_code": exc.status_code,
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
