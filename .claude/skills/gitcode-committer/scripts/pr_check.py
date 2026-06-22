#!/usr/bin/env python3
# coding: utf-8
"""
GitCode PR 合并设置检查脚本。

检查：
1. close_related_issue 设置
2. 关联的 issue
3. 生成 squash commit message 建议

用法：
    gitcode-check <N>
    gitcode-check <N> --squash
"""

import os
import sys
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

from pr_client import PRClient, parse_pr_reference
from pr_commenter import check_pr_merge_settings, format_squash_message


def check_and_report(
    client: PRClient,
    number: int,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    show_squash: bool = False,
    fix: bool = False,
) -> None:
    """检查 PR 合并设置并报告。

    Args:
        client: PRClient 实例。
        number: PR 编号。
        owner: 仓库 owner。
        repo: 仓库名。
        show_squash: 是否显示 squash message 建议。
        fix: 是否自动修复问题。
    """
    owner = owner or client.upstream_owner
    repo = repo or client.upstream_repo

    print(f"## PR #{number} 合并设置检查\n")

    # 获取设置
    settings = check_pr_merge_settings(client, number, owner, repo)

    # 输出基本状态
    close_related = settings.get("close_related_issue")
    has_issues = settings.get("has_issues")
    issues = settings.get("linked_issues", [])
    pr_title = settings.get("pr_title", "")

    print(f"**PR 标题**: {pr_title}\n")

    # 检查 close_related_issue
    if close_related is True:
        print("❌ **关闭关联 issue**: 已开启")
        if fix:
            try:
                client.update_pr_settings(
                    number, close_related_issue=False, owner=owner, repo=repo
                )
                print("   ✅ 已自动关闭此选项")
            except Exception as e:
                print(f"   ⚠️ 无法修改：{e}")
    elif close_related is False:
        print("✅ **关闭关联 issue**: 未开启")
    else:
        print("ℹ️ **关闭关联 issue**: 未设置（将使用仓库默认配置）")

    # 检查关联 issue
    if has_issues:
        print(f"\n✅ **关联 issue**: {len(issues)} 个")
        for issue_num in issues:
            print(f"   - #{issue_num}")
    else:
        print("\n⚠️ **关联 issue**: 无")
        print("   💡 建议：在 PR 描述中添加 `Fixes #issue号` 或创建对应 issue")

    # 显示 squash message 建议
    if show_squash or not has_issues or close_related is True:
        print(f"\n### 📝 Squash Commit Message 建议\n")
        squash_msg = format_squash_message(pr_title, issues)
        print("```")
        print(squash_msg)
        print("```")

    # 总结
    warnings = settings.get("warnings", [])
    if warnings:
        print("\n### ⚠️ 需要注意\n")
        for w in warnings:
            print(f"- {w}")

    if not warnings:
        print("\n✅ **所有检查通过**，可以合并")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="检查 PR 合并设置")
    parser.add_argument("number", help="PR 编号或链接")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认用 .claude/skills/gitcode-config.json）",
    )
    parser.add_argument("--owner", help="仓库 owner")
    parser.add_argument("--repo", help="仓库名")
    parser.add_argument(
        "--squash",
        action="store_true",
        help="显示 squash commit message 建议",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="自动修复问题（如关闭 close_related_issue）",
    )

    args = parser.parse_args()

    client = PRClient.from_config(args.config)

    owner, repo, number = parse_pr_reference(args.number)
    owner = owner or args.owner or client.upstream_owner
    repo = repo or args.repo or client.upstream_repo

    check_and_report(
        client, number, owner, repo,
        show_squash=args.squash,
        fix=args.fix,
    )
