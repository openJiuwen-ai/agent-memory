# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""检索内部的只读子树展开契约；不新增公开 MemoryAPI.expand。

传入已物化的 root，按完整 Scope + id 遍历有序引用。select 由检索编排注入，
负责披露塑形及共享预算准入；返回 False 时停止遍历，不继续读取该分支。
Expander 不查询索引、不计算相关性、不生成或修复边。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, ParsedQuery, Scope

from .base import RetrievalOperator

NodeKey = tuple[str, str, str, str, str, str]


class _NodeKeyValue(NamedTuple):
    """保持六元 tuple 兼容的节点身份值。"""

    org: str
    space: str
    user: str
    agent: str
    session: str
    unit_id: str


MAX_EXPANSION_NODES = 1000


def node_key(unit: MemoryUnit) -> NodeKey:
    """节点身份不使用裸 id，允许不同细粒度 Scope 下的同名节点。"""
    scope = unit.scope
    return _NodeKeyValue(scope.org, scope.space, scope.user, scope.agent, scope.session, unit.id)


def within_scope(candidate: Scope, boundary: Scope) -> bool:
    """org/space 严格相等，已授权边界的非空主体与 session 维度继续收窄。"""
    if candidate.org != boundary.org or candidate.space != boundary.space:
        return False
    for dimension in ("user", "agent", "session"):
        value = getattr(boundary, dimension)
        if value and getattr(candidate, dimension) != value:
            return False
    return True


@dataclass
class ExpandRequest:
    """单根展开请求；query 已移除 typed 父角色条件，但保留全部可见性过滤。"""

    root: MemoryUnit
    query: ParsedQuery
    depth: int
    select: Callable[[MemoryUnit, int], bool]
    seen: set[NodeKey] = field(default_factory=set)
    node_limit: int = MAX_EXPANSION_NODES


@dataclass(frozen=True)
class ExpandIssue:
    """稳定诊断码；不携带被排除节点的正文、Scope 或真实存在性信息。"""

    code: str


@dataclass
class ExpandResult:
    """处理深度与截断状态；到达请求深度本身不算截断。"""

    actual_depth: int = 0
    selected_count: int = 0
    visited_count: int = 0
    truncated: bool = False
    complete: bool = True
    issues: list[ExpandIssue] = field(default_factory=list)

    def exclude(self, code: str, *, truncated: bool = False) -> None:
        """保留首次遇到的诊断顺序，不以坏节点数量放大响应。"""
        issue = ExpandIssue(code)
        if issue not in self.issues:
            self.issues.append(issue)
        self.complete = False
        self.truncated = self.truncated or truncated


class ExpanderProducer(Factory):
    """展开算子的工厂；读取数据面由 Retriever 装配时注入，不另解析存储。"""

    TOP_NAME = "expander"


class Expander(RetrievalOperator):
    """在已授权范围内遍历，不扩大权限、不写入或修复记忆。"""

    @abstractmethod
    def expand(self, scope: Scope, request: ExpandRequest) -> ExpandResult:
        """按层读取后代并交给 select；返回结果不包含根节点。"""
