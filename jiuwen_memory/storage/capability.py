# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Store 级能力声明（F09 工作包 A）。

存储层此前只有 ``StoreManager.capabilities()`` 这一层端口能力；分片路由需要的
细粒度能力（条件 KV、批量定位、实体增量解除关联、共享索引前置过滤……）是**单个
Store 实例**的属性，必须在契约层显式声明，而不是靠 ``isinstance`` 或 target 名
猜测（F09 D6 / D12 / S09 #9）。

实现约定：
- 能力是**静态、装配期固定**的事实；运行期不撤销、不按请求切换。
- 未声明某能力的实现收到需要该能力的请求时，必须抛
  :class:`~common.errors.UnsupportedStorageCapabilityError`（统一错误类型），
  由装配层在启动期尽早编译期校验，避免把「不支持」拖到运行期静默降级。
"""

from __future__ import annotations

from enum import Enum

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError


class StoreCapability(str, Enum):
    """单个 Store 实例可声明的能力（值即对外标识，稳定）。"""

    #: 原子创建、按完整值的条件替换 / 条件删除、批量部分读取、存储侧当前时间
    CONDITIONAL_KV = "conditional_kv"
    #: 批量 (scope, unit_id) → 分片定位
    SHARD_LOCATOR = "shard_locator"
    #: 本端口是分片路由包装层（装配期支持矩阵据此判定是否处于分片部署）
    SHARD_ROUTED = "shard_routed"
    #: 本端口持有归属台账与拓扑登记（分片 KV 包装层）；装配器据此选出登记 owner
    SHARD_PLACEMENT = "shard_placement"
    #: 增量解除关联是单条记录内的原子操作
    ATOMIC_UNLINK = "atomic_unlink"
    #: 实体关联按分片分组保存，可按分片过滤
    GROUPED_LINKS = "grouped_links"
    #: 共享索引支持在截断前下推分片过滤
    SHARD_PREFILTER = "shard_prefilter"
    #: 后端可声明规范化物理资源标识（不含凭据）
    RESOURCE_DESCRIPTOR = "resource_descriptor"
    #: 后端可按 metadata.unit_id 找出该记忆的全部记录 id（F09 D16）
    UNIT_RECORD_LOOKUP = "unit_record_lookup"
    #: 分片包装层支持搬迁 / 清理单条记忆的记录（F09 D16）
    SHARD_RELOCATABLE = "shard_relocatable"


def require_capability(
    store: object,
    capability: StoreCapability,
    *,
    role: str = "",
) -> None:
    """校验 ``store`` 声明了 ``capability``；缺失抛统一不支持错误。

    ``role`` 用于错误消息（如 ``"placement_store"`` / ``"entity inner"``），
    不参与判定。
    """
    declared = getattr(store, "capabilities", None)
    caps = declared() if callable(declared) else frozenset()
    if capability not in caps:
        name = role or type(store).__name__
        raise UnsupportedStorageCapabilityError(
            f"{name} does not declare storage capability {capability.value!r}"
        )


def capabilities_of(store: object) -> frozenset[StoreCapability]:
    """读取 Store 声明的能力集合（未实现声明时视为空）。"""
    declared = getattr(store, "capabilities", None)
    if not callable(declared):
        return frozenset()
    return frozenset(declared())
