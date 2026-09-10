# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""树结构构建契约：调用方备齐输入，Composer 派生父层并经 IndexBuilder 保存。

叶是权威事实，父是可重建派生物；建树只改变叶的 hierarchy，不改变正文、时间、
来源或生命周期。节点由完整 Scope + id 定位，父可以驻留在更粗的 tree_home_scope。
支持 TIME 的 snapshot → time_span → scene → event 前缀，不自行查库、鉴权或调度任务。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, MemoryUnit, Scope

from .base import ConstructionOperator

TIME_PARENT_ROLES = (HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT)
TIME_CHILD_ROLES = {
    HierarchyRole.TIME_SPAN: HierarchyRole.SNAPSHOT,
    HierarchyRole.SCENE: HierarchyRole.TIME_SPAN,
    HierarchyRole.EVENT: HierarchyRole.SCENE,
}


@dataclass(frozen=True)
class HierarchyComposeProfile:
    """装配期的固定树形和分组配置，变更后通过显式重建生效。"""

    kind: HierarchyKind
    leaf_role: HierarchyRole
    parent_roles: tuple[HierarchyRole, ...]
    stage_options: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class HierarchyComposeOptions:
    """单次请求的树形、父归属、可选区间及仅附加到新父的系统元数据。"""

    kind: HierarchyKind
    leaf_role: HierarchyRole
    parent_roles: list[HierarchyRole]
    tree_home_scope: Scope
    span_start: datetime | None = None
    span_end: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class HierarchyComposeRequest:
    """完整输入；替换时须包含相交旧根的全部父层和 snapshot，Composer 不查询补齐。"""

    leaves: list[MemoryUnit]
    options: HierarchyComposeOptions
    existing_parents: list[MemoryUnit] = field(default_factory=list)


def validate_time_parent_roles(roles: list[HierarchyRole]) -> None:
    """请求只接受 TIME_SPAN → SCENE → EVENT 的非空严格枚举前缀。"""
    chain = TIME_PARENT_ROLES
    if not isinstance(roles, list) or not 1 <= len(roles) <= len(chain):
        raise ValidationError("parent_roles 必须是 [TIME_SPAN, SCENE, EVENT] 的非空前缀")
    if any(role is not chain[position] for position, role in enumerate(roles)):
        raise ValidationError("parent_roles 必须按 TIME_SPAN、SCENE、EVENT 顺序使用 HierarchyRole")


@dataclass
class HierarchyRepair:
    """失败报告，不携带自动修复动作；同名 id 的定位须结合原请求 Scope。"""

    unit_id: str
    issue: str
    expected_parent_id: str = ""
    observed_parent_id: str = ""


@dataclass
class HierarchyComposeResult:
    """已完成的写入与失败项；complete=False 时不得把整次构建标记成功。"""

    created_parent_ids: list[str] = field(default_factory=list)
    updated_child_ids: list[str] = field(default_factory=list)
    replaced_parent_ids: list[str] = field(default_factory=list)
    repair_required: list[HierarchyRepair] = field(default_factory=list)
    complete: bool = True


class HierarchyComposerProducer(Factory):
    """树构建算子的注册式工厂，实例依赖使用 hierarchy_composer 命名空间。"""

    TOP_NAME = "hierarchy_composer"


class HierarchyComposer(ConstructionOperator):
    """校验输入 → 生成候选 → 校验候选树 → 持久化的统一入口。"""

    @abstractmethod
    def build(self, request: HierarchyComposeRequest) -> HierarchyComposeResult:
        """首次建树，拒绝已有父引用或请求携带旧父。"""

    @abstractmethod
    def replace_in_span(self, request: HierarchyComposeRequest) -> HierarchyComposeResult:
        """替换与有界区间相交的旧父层，权威叶内容不变。"""
