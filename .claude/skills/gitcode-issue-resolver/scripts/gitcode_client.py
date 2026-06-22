#!/usr/bin/env python3
# coding: utf-8
"""
GitCode API v5 客户端。

封装 GitCode REST API 交互，支持 upstream + fork 双仓库上下文。
- Issue 操作（获取、评论、标签）→ upstream 仓库
- PR/MR 创建 → upstream 仓库，head 来自 fork
- 认证优先级：环境变量 GITCODE_TOKEN → 配置文件 → 交互提示
"""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional
try:
    import requests
except ImportError:
    print(
        "错误: 缺少 requests 库，请执行: "
        "pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)


class GitCodeClientError(Exception):
    """GitCode API 调用异常。"""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GitCodeClient:
    """GitCode API v5 客户端。

    支持 upstream（主仓）和 fork（个人仓）双仓库上下文。
    Issue 操作针对 upstream，分支/推送针对 fork，
    MR 从 fork 提到 upstream。
    """

    BASE_URL = "https://gitcode.com/api/v5"
    MAX_RETRIES = 3
    RETRY_WAIT_SECONDS = 15

    def __init__(
        self,
        token: str,
        upstream_owner: str,
        upstream_repo: str,
        fork_owner: str = "",
        fork_repo: str = "",
        base_branch: str = "main",
    ):
        """初始化客户端。

        Args:
            token: GitCode access token。
            upstream_owner: 主仓 owner。
            upstream_repo: 主仓 repo 名称。
            fork_owner: 个人 fork 的 owner。
            fork_repo: 个人 fork 的 repo 名称。
            base_branch: 主仓默认分支。
        """
        self.token = token
        self.upstream_owner = upstream_owner
        self.upstream_repo = upstream_repo
        self.fork_owner = fork_owner or upstream_owner
        self.fork_repo = fork_repo or upstream_repo
        self.base_branch = base_branch
        self._session = requests.Session()

    # ── Issue 操作（针对 upstream） ──────────────

    def get_issue(self, number: int) -> Dict[str, Any]:
        """获取 upstream 仓库的某个 Issue。

        Args:
            number: Issue 编号。

        Returns:
            Issue 详情字典。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}/issues/{number}"
        )
        return self._request("GET", path)

    def list_issues(
        self,
        state: str = "open",
        labels: str = "",
        page: int = 1,
        per_page: int = 20,
        assignee: str = "",
    ) -> List[Dict[str, Any]]:
        """获取 upstream 仓库的 Issue 列表。

        Args:
            state: Issue 状态（open/closed/all）。
            labels: 逗号分隔的标签过滤。
            page: 页码。
            per_page: 每页数量。
            assignee: 指派人用户名过滤。

        Returns:
            Issue 列表。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}/issues"
        )
        params: Dict[str, Any] = {
            "state": state,
            "page": page,
            "per_page": per_page,
        }
        if labels:
            params["labels"] = labels
        if assignee:
            params["assignee"] = assignee
        return self._request("GET", path, params=params)

    def get_issue_comments(
        self,
        number: int,
        page: int = 1,
        per_page: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取 Issue 的所有评论。

        Args:
            number: Issue 编号。
            page: 页码。
            per_page: 每页数量。

        Returns:
            评论列表。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}"
            f"/issues/{number}/comments"
        )
        params = {"page": page, "per_page": per_page}
        return self._request("GET", path, params=params)

    def create_comment(
        self,
        number: int,
        body: str,
    ) -> Dict[str, Any]:
        """在 Issue 中创建评论。

        Args:
            number: Issue 编号。
            body: 评论内容（支持 Markdown）。

        Returns:
            创建的评论详情。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}"
            f"/issues/{number}/comments"
        )
        return self._request(
            "POST", path, json_data={"body": body}
        )

    def update_issue(
        self,
        number: int,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """更新 Issue 信息。

        Args:
            number: Issue 编号。
            **kwargs: 可更新字段（title, body, state,
                assignee 等）。

        Returns:
            更新后的 Issue 详情。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/issues/{number}"
        )
        data = {
            "repo": self.upstream_repo,
        }
        data.update(kwargs)
        return self._request("PATCH", path, json_data=data)

    def add_labels(
        self,
        number: int,
        labels: List[str],
    ) -> List[Dict[str, Any]]:
        """为 Issue 添加标签。

        Args:
            number: Issue 编号。
            labels: 标签名称列表。

        Returns:
            标签列表。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}"
            f"/issues/{number}/labels"
        )
        return self._request(
            "POST", path, json_data=labels
        )

    # ── PR/MR 操作（upstream 仓库，head 来自 fork） ──

    def create_pull_request(
        self,
        title: str,
        head: str,
        base: str = "",
        body: str = "",
    ) -> Dict[str, Any]:
        """创建从 fork 到 upstream 的 Pull Request。

        Args:
            title: PR 标题。
            head: 源分支（格式：fork_owner/fork_repo:branch）。
            base: 目标分支，默认 base_branch。
            body: PR 描述。

        Returns:
            创建的 PR 详情。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}/pulls"
        )
        data = {
            "title": title,
            "head": head,
            "base": base or self.base_branch,
            "body": body,
        }
        return self._request("POST", path, json_data=data)

    def get_pull_request(self, number: int) -> Dict[str, Any]:
        """获取 upstream 仓库的某个 Pull Request。

        Args:
            number: PR 编号。

        Returns:
            PR 详情字典。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}/pulls/{number}"
        )
        return self._request("GET", path)

    def update_pull_request(
        self,
        number: int,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """更新 Pull Request 信息。

        Args:
            number: PR 编号。
            **kwargs: 可更新字段（title, body, state,
                target_branch 等）。

        Note:
            GitCode API 不支持更新 head（源分支）。
            如需换分支，应创建新 PR。
            关闭/重开使用 state 字段（"closed"/"open"），
            且 PATCH 至少需带一个其它字段（如 title）。

        Returns:
            更新后的 PR 详情。
        """
        path = (
            f"/repos/{self.upstream_owner}"
            f"/{self.upstream_repo}/pulls/{number}"
        )
        return self._request("PATCH", path, json_data=kwargs)

    def close_pull_request(
        self,
        number: int,
    ) -> Dict[str, Any]:
        """关闭 Pull Request。

        GitCode（Gitea 风格）使用 state="closed" 关闭 PR，
        而非 GitLab 的 state_event。PATCH 接口要求至少携带一个
        非 state 字段，因此这里回填当前 title 一起提交。
        若 PR 已非 opened/locked 状态，直接返回当前详情，
        避免对已关闭 PR 重复关闭时 GitCode 报 400。

        Args:
            number: PR 编号。

        Returns:
            关闭后的 PR 详情。
        """
        current = self.get_pull_request(number)
        if current.get("state") not in ("opened", "open", "locked"):
            return current
        title = current.get("title", "")
        return self.update_pull_request(
            number, state="closed", title=title
        )

    # ── 内部方法 ────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Any = None,
    ) -> Any:
        """发送 API 请求，处理认证和限流。

        Args:
            method: HTTP 方法。
            path: API 路径（不含 BASE_URL）。
            params: 查询参数。
            json_data: 请求体 JSON 数据。

        Returns:
            响应 JSON。

        Raises:
            GitCodeClientError: API 调用失败。
        """
        url = f"{self.BASE_URL}{path}"
        if params is None:
            params = {}
        params["access_token"] = self.token

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_WAIT_SECONDS)
                    continue
                raise GitCodeClientError(
                    f"请求失败: {exc}"
                ) from exc

            if resp.status_code == 429:
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.RETRY_WAIT_SECONDS * (
                        attempt + 1
                    )
                    print(
                        f"触发限流，等待 {wait}s 后重试...",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                raise GitCodeClientError(
                    "API 限流，已达最大重试次数",
                    status_code=429,
                )

            if resp.status_code >= 400:
                raise GitCodeClientError(
                    f"API 错误 {resp.status_code}: "
                    f"{resp.text[:500]}",
                    status_code=resp.status_code,
                    response_body=resp.text,
                )

            if resp.status_code == 204:
                return {}
            return resp.json()

        raise GitCodeClientError("已达最大重试次数")

    # ── 工厂方法 ────────────────────────────

    @classmethod
    def from_config(
        cls,
        config_path: Optional[str] = None,
    ) -> "GitCodeClient":
        """从配置文件和环境变量创建客户端。

        认证优先级：
        1. 环境变量 GITCODE_TOKEN
        2. 配置文件中的 gitcode_token 字段
        3. 交互式提示输入

        仓库信息优先级：
        1. 配置文件（gitcode-config.json）
        2. .git/config 中的 git remote 自动发现
        3. 交互式提示输入

        Args:
            config_path: 配置文件路径。

        Returns:
            GitCodeClient 实例。
        """
        config: Dict[str, Any] = {}
        if config_path and os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

        upstream = config.get("upstream", {})
        fork = config.get("fork", {})

        # Token: 优先读取 GITCODE_TOKEN
        token = os.environ.get("GITCODE_TOKEN", "")
        if not token:
            token = config.get("gitcode_token", "")
        if not token:
            token = input("请输入 GitCode Token: ").strip()
        if not token:
            print(
                "错误: 未提供 GitCode Token",
                file=sys.stderr,
            )
            sys.exit(1)

        upstream_owner = upstream.get(
            "owner", config.get("owner", "")
        )
        upstream_repo = upstream.get(
            "repo", config.get("repo", "")
        )
        fork_owner = fork.get("owner", "")
        fork_repo = fork.get(
            "repo",
            upstream.get(
                "repo", config.get("repo", "")
            ),
        )
        base_branch = upstream.get(
            "base_branch",
            config.get("base_branch", "main"),
        )

        # 如果配置文件缺少 upstream/fork 信息，尝试从 .git/config 自动发现
        if not upstream_owner or not upstream_repo:
            auto = cls._discover_from_git_remote()
            if auto:
                if not upstream_owner:
                    upstream_owner = auto["upstream_owner"]
                if not upstream_repo:
                    upstream_repo = auto["upstream_repo"]
                if not fork_owner:
                    fork_owner = auto["fork_owner"]
                if not fork_repo or fork_repo == upstream_repo:
                    fork_repo = auto["fork_repo"]

        return cls(
            token=token,
            upstream_owner=upstream_owner,
            upstream_repo=upstream_repo,
            fork_owner=fork_owner,
            fork_repo=fork_repo,
            base_branch=base_branch,
        )

    @staticmethod
    def _discover_from_git_remote() -> Optional[Dict[str, str]]:
        """从 .git/config 中的 git remote 自动发现仓库信息。

        规则（与各 GitCode skill 的 shell 约定保持一致）：
        - 主仓：优先名为 ``upstream`` 的 remote，否则用 ``origin``
        - fork：除主仓外的另一个 remote
        - 只有一个 remote 时，无法确定 fork

        Returns:
            包含 upstream_owner, upstream_repo, fork_owner, fork_repo 的字典，
            或 None（无法发现）。
        """
        try:
            import subprocess

            result = subprocess.run(
                ["git", "remote", "-v"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None

            # 先收集 remote 名 → (owner, repo) 映射
            remotes: Dict[str, tuple[str, str]] = {}
            for line in result.stdout.strip().split("\n"):
                if not line.strip() or "(fetch)" not in line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                name = parts[0].rstrip()
                url = parts[1].strip()

                # 从 SSH URL: git@gitcode.com:owner/repo.git
                # 或 HTTPS URL: https://gitcode.com/owner/repo.git
                for prefix in [
                    "git@gitcode.com:",
                    "https://gitcode.com/",
                    "http://gitcode.com/",
                ]:
                    if url.startswith(prefix):
                        repo_path = url[len(prefix):].replace(".git", "")
                        path_parts = repo_path.split("/")
                        if len(path_parts) == 2:
                            remotes[name] = (path_parts[0], path_parts[1])
                        break

            if not remotes:
                return None

            # 主仓：优先 upstream，否则 origin，否则取任一
            if "upstream" in remotes:
                upstream_name = "upstream"
            elif "origin" in remotes:
                upstream_name = "origin"
            else:
                upstream_name = next(iter(remotes))
            upstream_owner, upstream_repo = remotes[upstream_name]

            # fork：除主仓外的另一个 remote（缺省回退主仓 repo 名）
            fork_owner = ""
            fork_repo = ""
            for name, (owner, repo_name) in remotes.items():
                if name != upstream_name:
                    fork_owner, fork_repo = owner, repo_name
                    break

            if upstream_owner and upstream_repo:
                return {
                    "upstream_owner": upstream_owner,
                    "upstream_repo": upstream_repo,
                    "fork_owner": fork_owner,
                    "fork_repo": fork_repo or upstream_repo,
                }
            return None

        except Exception:
            return None
