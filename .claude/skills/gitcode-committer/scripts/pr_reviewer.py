#!/usr/bin/env python3
# coding: utf-8
"""
GitCode PR 检视脚本。

获取 PR 信息，分析变更，生成检视报告。
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

from pr_client import PRClient, parse_pr_reference


def get_pr_summary(pr: Dict[str, Any]) -> Dict[str, Any]:
    """提取 PR 关键信息摘要。"""
    return {
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "state": pr.get("state", ""),
        "user": pr.get("user", {}).get("login", ""),
        "head": pr.get("head", {}).get("label", ""),
        "base": pr.get("base", {}).get("label", ""),
        "created_at": pr.get("created_at", ""),
        "updated_at": pr.get("updated_at", ""),
        "merged": pr.get("merged", False),
        "mergeable": pr.get("mergeable"),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "html_url": pr.get("html_url", ""),
        "body": pr.get("body", ""),
    }


def analyze_file_changes(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """分析文件变更情况。"""
    result = {
        "total": len(files),
        "additions": 0,
        "deletions": 0,
        "by_type": {
            "added": [],
            "modified": [],
            "removed": [],
            "renamed": [],
        },
        "by_ext": {},
    }

    for f in files:
        status = f.get("status", "modified")
        filename = f.get("filename", "")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)

        result["additions"] += additions
        result["deletions"] += deletions

        # 按状态分类
        if status == "added":
            result["by_type"]["added"].append(filename)
        elif status == "removed":
            result["by_type"]["removed"].append(filename)
        elif status == "renamed":
            result["by_type"]["renamed"].append(
                (f.get("previous_filename", ""), filename)
            )
        else:
            result["by_type"]["modified"].append(filename)

        # 按扩展名分类
        ext = os.path.splitext(filename)[1] or "无扩展名"
        result["by_ext"].setdefault(ext, []).append(
            {"file": filename, "additions": additions, "deletions": deletions}
        )

    return result


def format_review_report(
    pr_summary: Dict[str, Any],
    file_analysis: Dict[str, Any],
    commits: List[Dict[str, Any]],
    comments: List[Dict[str, Any]],
) -> str:
    """格式化检视报告。"""
    lines = []

    # 基本信息
    lines.append(f"## PR #{pr_summary['number']} 检视报告")
    lines.append("")
    lines.append("### 基本信息")
    lines.append(f"- **标题**: {pr_summary['title']}")
    lines.append(f"- **状态**: {pr_summary['state']}")
    lines.append(
        f"- **分支**: `{pr_summary['head']}` → `{pr_summary['base']}`"
    )
    lines.append(f"- **作者**: {pr_summary['user']}")
    lines.append("")

    # 变更概览
    lines.append("### 变更概览")
    lines.append(f"- **文件数**: {file_analysis['total']} 个文件")
    lines.append(f"- **Commits**: {len(commits)} 个提交")
    lines.append(
        f"- **变更行**: +{file_analysis['additions']} "
        f"-{file_analysis['deletions']}"
    )
    lines.append("")

    # 变更文件
    lines.append("### 变更文件")

    if file_analysis["by_type"]["added"]:
        lines.append("#### 新增文件")
        for f in file_analysis["by_type"]["added"]:
            lines.append(f"- 🆕 `{f}`")
        lines.append("")

    if file_analysis["by_type"]["modified"]:
        lines.append("#### 修改文件")
        for f in file_analysis["by_type"]["modified"]:
            lines.append(f"- ✏️ `{f}`")
        lines.append("")

    if file_analysis["by_type"]["removed"]:
        lines.append("#### 删除文件")
        for f in file_analysis["by_type"]["removed"]:
            lines.append(f"- ❌ `{f}`")
        lines.append("")

    if file_analysis["by_type"]["renamed"]:
        lines.append("#### 重命名文件")
        for old, new in file_analysis["by_type"]["renamed"]:
            lines.append(f"- 🔀 `{old}` → `{new}`")
        lines.append("")

    # 按文件类型分组
    lines.append("#### 按文件类型")
    for ext, files in sorted(file_analysis["by_ext"].items()):
        lines.append(f"\n**{ext or '无扩展名'}** ({len(files)} 个文件)")
        for f in files:
            lines.append(
                f"  - `{f['file']}`: +{f['additions']}/-{f['deletions']}"
            )
    lines.append("")

    # Commits
    if commits:
        lines.append("### 提交历史")
        for c in commits[:10]:  # 只显示最近 10 个
            sha = c.get("sha", "")[:7]
            msg = c.get("commit", {}).get("message", "").split("\n")[0]
            author = c.get("commit", {}).get("author", {}).get("name", "")
            lines.append(f"- `{sha}` {msg} ({author})")
        if len(commits) > 10:
            lines.append(f"  ... 还有 {len(commits) - 10} 个提交")
        lines.append("")

    # 已有评论
    if comments:
        lines.append("### 已有评论")
        for c in comments[:5]:  # 只显示最近 5 条
            user = c.get("user", {}).get("login", "")
            body = c.get("body", "")[:100]
            if len(c.get("body", "")) > 100:
                body += "..."
            lines.append(f"- **{user}**: {body}")
        if len(comments) > 5:
            lines.append(f"  ... 还有 {len(comments) - 5} 条评论")
        lines.append("")

    # PR 描述
    if pr_summary["body"]:
        lines.append("### PR 描述")
        lines.append("```")
        lines.append(pr_summary["body"])
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append(f"🔗 {pr_summary['html_url']}")

    return "\n".join(lines)


def review_pr(
    client: PRClient,
    number: int,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    """检视 PR，返回结构化信息。

    Args:
        client: PRClient 实例。
        number: PR 编号。
        owner: 仓库 owner。
        repo: 仓库名。

    Returns:
        包含 PR 信息、文件分析、commits、评论的字典。
    """
    # 获取 PR 详情
    pr = client.get_pull_request(number, owner, repo)
    pr_summary = get_pr_summary(pr)

    # 获取文件变更
    files = client.get_pull_request_files(number, owner, repo)
    file_analysis = analyze_file_changes(files)

    # 获取 commits
    commits = client.get_pull_request_commits(number, owner, repo)

    # 获取评论
    comments = client.get_pull_request_comments(number, owner, repo)

    return {
        "pr": pr_summary,
        "files": files,
        "file_analysis": file_analysis,
        "commits": commits,
        "comments": comments,
        "report": format_review_report(
            pr_summary, file_analysis, commits, comments
        ),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GitCode PR 检视")
    parser.add_argument("number", help="PR 编号或链接")
    parser.add_argument(
        "--config", default=None, help="配置文件路径（默认用 .claude/skills/gitcode-config.json）"
    )
    parser.add_argument("--owner", help="仓库 owner")
    parser.add_argument("--repo", help="仓库名")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")

    args = parser.parse_args()

    client = PRClient.from_config(args.config)

    owner, repo, number = parse_pr_reference(args.number)
    owner = owner or args.owner or client.upstream_owner
    repo = repo or args.repo or client.upstream_repo

    result = review_pr(client, number, owner, repo)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result["report"])
