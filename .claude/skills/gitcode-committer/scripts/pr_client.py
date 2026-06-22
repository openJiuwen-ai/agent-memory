#!/usr/bin/env python3
# coding: utf-8
"""
GitCode Pull Request API 客户端。

扩展 GitCodeClient，支持 PR/MR 操作。
- 获取 PR 详情、文件、commits、评论
- 创建 PR 评论
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

# 添加 gitcode-issue-resolver 脚本目录到路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))  # skills/
ISSUE_RESOLVER_DIR = os.path.join(SKILLS_DIR, "gitcode-issue-resolver", "scripts")
if ISSUE_RESOLVER_DIR not in sys.path:
    sys.path.append(ISSUE_RESOLVER_DIR)

try:
    from gitcode_client import GitCodeClient, GitCodeClientError
except ImportError:
    print(
        "错误: 无法导入 gitcode_client，"
        "请确保 gitcode-issue-resolver 技能已安装",
        file=sys.stderr,
    )
    sys.exit(1)


class PRClient(GitCodeClient):
    """Pull Request/Merge Request 操作客户端。"""

    # ── PR/MR 操作 ────────────────────────────

    def get_pull_request(
        self,
        number: int,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取 PR 详情。

        Args:
            number: PR 编号。
            owner: 仓库 owner（默认 upstream）。
            repo: 仓库名（默认 upstream）。

        Returns:
            PR 详情字典。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path = f"/repos/{owner}/{repo}/pulls/{number}"
        return self._request("GET", path)

    def get_pull_request_files(
        self,
        number: int,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取 PR 变更文件列表。

        Args:
            number: PR 编号。
            owner: 仓库 owner。
            repo: 仓库名。

        Returns:
            文件变更列表。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path = f"/repos/{owner}/{repo}/pulls/{number}/files"
        return self._request("GET", path)

    def get_pull_request_commits(
        self,
        number: int,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取 PR commits 列表。

        Args:
            number: PR 编号。
            owner: 仓库 owner。
            repo: 仓库名。

        Returns:
            Commits 列表。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path = f"/repos/{owner}/{repo}/pulls/{number}/commits"
        return self._request("GET", path)

    def get_pull_request_comments(
        self,
        number: int,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取 PR 评论列表。

        Args:
            number: PR 编号。
            owner: 仓库 owner。
            repo: 仓库名。
            page: 页码。
            per_page: 每页数量。

        Returns:
            评论列表。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path = f"/repos/{owner}/{repo}/pulls/{number}/comments"
        return self._request(
            "GET", path, params={"page": page, "per_page": per_page}
        )

    def create_pull_request_comment(
        self,
        number: int,
        body: str,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        commit_id: Optional[str] = None,
        path: Optional[str] = None,
        position: Optional[int] = None,
    ) -> Dict[str, Any]:
        """创建 PR 评论。

        Args:
            number: PR 编号。
            body: 评论内容（支持 Markdown）。
            owner: 仓库 owner。
            repo: 仓库名。
            commit_id: 关联的 commit SHA（行内评论）。
            path: 文件路径（行内评论）。
            position: 行位置（行内评论）。

        Returns:
            创建的评论详情。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path_api = f"/repos/{owner}/{repo}/pulls/{number}/comments"

        data: Dict[str, Any] = {"body": body}
        if commit_id:
            data["commit_id"] = commit_id
        if path:
            data["path"] = path
        if position is not None:
            data["position"] = position

        return self._request("POST", path_api, json_data=data)

    def create_issue_comment(
        self,
        number: int,
        body: str,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建 PR 评论。

        GitCode 使用 GitHub 风格的 pulls 端点创建 PR 评论。

        Args:
            number: PR 编号。
            body: 评论内容。
            owner: 仓库 owner。
            repo: 仓库名。

        Returns:
            创建的评论详情。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        # GitCode PR 评论用 pulls 端点
        path = f"/repos/{owner}/{repo}/pulls/{number}/comments"
        return self._request("POST", path, json_data={"body": body})

    # ── PR 合并设置 ────────────────────────────

    def get_linked_issues(
        self,
        number: int,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取 PR 关联的 issue 列表。

        Args:
            number: PR 编号。
            owner: 仓库 owner。
            repo: 仓库名。

        Returns:
            关联的 issue 列表。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path = f"/repos/{owner}/{repo}/pulls/{number}/issues"
        return self._request("GET", path)

    def update_pr_settings(
        self,
        number: int,
        close_related_issue: Optional[bool] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """更新 PR 设置。

        Args:
            number: PR 编号。
            close_related_issue: 合并后是否关闭关联的 Issue。
            title: PR 标题。
            body: PR 描述。
            owner: 仓库 owner。
            repo: 仓库名。

        Returns:
            更新后的 PR 详情。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path = f"/repos/{owner}/{repo}/pulls/{number}"

        data: Dict[str, Any] = {}
        if close_related_issue is not None:
            data["close_related_issue"] = close_related_issue
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body

        return self._request("PATCH", path, json_data=data)

    def get_pr_merge_status(
        self,
        number: int,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取 PR 合并状态。

        Args:
            number: PR 编号。
            owner: 仓库 owner。
            repo: 仓库名。

        Returns:
            合并状态信息。
        """
        owner = owner or self.upstream_owner
        repo = repo or self.upstream_repo
        path = f"/repos/{owner}/{repo}/pulls/{number}/merge"
        return self._request("GET", path)

    # ── 工厂方法 ────────────────────────────

    @classmethod
    def from_config(
        cls,
        config_path: Optional[str] = None,
    ) -> "PRClient":
        """从配置文件和环境变量创建客户端。

        Args:
            config_path: 配置文件路径。为 None 时回退到共享配置
                .claude/skills/gitcode-config.json。

        Returns:
            PRClient 实例。
        """
        # 共享配置：.claude/skills/gitcode-config.json
        # SCRIPT_DIR = .claude/skills/gitcode-committer/scripts
        # 上溯两级即 .claude/skills/
        shared_config = os.path.join(
            os.path.dirname(os.path.dirname(SCRIPT_DIR)),
            "gitcode-config.json",
        )
        config: Dict[str, Any] = {}
        config_locations = [
            config_path,
            shared_config,
        ]

        for loc in config_locations:
            if loc and os.path.exists(loc):
                with open(loc, encoding="utf-8") as f:
                    config = json.load(f)
                break

        # 仓库信息：从 upstream 读取（共享配置单一来源）
        upstream = config.get("upstream", {})
        repo_owner = upstream.get("owner")
        repo_name = upstream.get("repo")

        # Token 来源：环境变量 > 共享配置 gitcode_token > 提示
        token = os.environ.get("GITCODE_TOKEN", "")
        if not token:
            token = config.get("gitcode_token", "")
        if not token:
            token = input("请输入 GitCode Token: ").strip()
        if not token:
            print("错误: 未提供 GitCode Token", file=sys.stderr)
            sys.exit(1)

        return cls(
            token=token,
            upstream_owner=repo_owner,
            upstream_repo=repo_name,
        )


def parse_pr_reference(ref: str) -> tuple:
    """解析 PR 引用，返回 (owner, repo, number)。

    支持格式：
    - 78
    - #78
    - https://gitcode.com/openJiuwen/deepsearch/pull/78
    - https://gitcode.com/openJiuwen/deepsearch/merge_requests/78

    Args:
        ref: PR 引用字符串。

    Returns:
        (owner, repo, number) 元组，owner/repo 可能为 None。
    """
    ref = ref.strip()

    # 纯数字
    if ref.isdigit():
        return (None, None, int(ref))

    # #78 格式
    if ref.startswith("#") and ref[1:].isdigit():
        return (None, None, int(ref[1:]))

    # URL 格式
    if ref.startswith("http"):
        # https://gitcode.com/openJiuwen/deepsearch/pull/78
        # https://gitcode.com/openJiuwen/deepsearch/merge_requests/78
        import re

        match = re.search(
            r"gitcode\.com/([^/]+)/([^/]+)/(?:pull|merge_requests)/(\d+)",
            ref,
        )
        if match:
            return (match.group(1), match.group(2), int(match.group(3)))

    raise ValueError(f"无法解析 PR 引用: {ref}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GitCode PR API 客户端")
    parser.add_argument("--get", metavar="NUMBER", help="获取 PR 详情")
    parser.add_argument("--files", metavar="NUMBER", help="获取 PR 文件列表")
    parser.add_argument("--commits", metavar="NUMBER", help="获取 PR commits")
    parser.add_argument(
        "--comments", metavar="NUMBER", help="获取 PR 评论"
    )
    parser.add_argument("--comment", nargs=2, metavar=("NUMBER", "BODY"),
                        help="创建 PR 评论")
    parser.add_argument(
        "--config", default=None, help="配置文件路径（默认用 .claude/skills/gitcode-config.json）"
    )
    parser.add_argument("--owner", help="仓库 owner")
    parser.add_argument("--repo", help="仓库名")

    args = parser.parse_args()

    client = PRClient.from_config(args.config)

    if args.get:
        owner, repo, number = parse_pr_reference(args.get)
        owner = owner or args.owner or client.upstream_owner
        repo = repo or args.repo or client.upstream_repo
        result = client.get_pull_request(number, owner, repo)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.files:
        owner, repo, number = parse_pr_reference(args.files)
        owner = owner or args.owner or client.upstream_owner
        repo = repo or args.repo or client.upstream_repo
        result = client.get_pull_request_files(number, owner, repo)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.commits:
        owner, repo, number = parse_pr_reference(args.commits)
        owner = owner or args.owner or client.upstream_owner
        repo = repo or args.repo or client.upstream_repo
        result = client.get_pull_request_commits(number, owner, repo)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.comments:
        owner, repo, number = parse_pr_reference(args.comments)
        owner = owner or args.owner or client.upstream_owner
        repo = repo or args.repo or client.upstream_repo
        result = client.get_pull_request_comments(number, owner, repo)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.comment:
        number_str, body = args.comment
        owner, repo, number = parse_pr_reference(number_str)
        owner = owner or args.owner or client.upstream_owner
        repo = repo or args.repo or client.upstream_repo
        result = client.create_issue_comment(number, body, owner, repo)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    else:
        parser.print_help()
