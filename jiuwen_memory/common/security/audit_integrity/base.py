# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AuditIntegrityProvider - 审计事件密码学完整性契约（F05 §Audit Integrity）。

Provider 负责：

1. 规范化 AuditEvent；
2. 依据前序链头、sequence、key ref 计算 proof；
3. 协调 ChainStore 的原子追加与有界冲突重试；
4. 流式验证指定稳定快照；
5. 校验外部锚点（若配置）；
6. 暴露 ``health()`` 和自身 capability。

Provider **不**负责普通审计过滤/query，不拥有 MemoryAPI，也不自行读取 YAML、
环境变量或根密钥--密钥一律经 KeyProvider 的 MAC capability 取得。

**接口先行说明**：本文件只固定契约。``audit_integrity_impl``（版本化规范化 +
链式 HMAC 实现等）随实装 PR 合入；本期 ``AuditIntegrityProducer`` 无注册 target，
配置 ``audit_integrity`` 段会因未注册实现而装配失败（fail-closed，不静默降级）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from jiuwen_memory.common.errors import AgentMemoryError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import AuditEvent

if TYPE_CHECKING:
    from jiuwen_memory.common.security.audit_integrity.chain_store import (
        ChainedAuditStore,
        ChainedRecord,
        ChainStoreCapability,
    )
    from jiuwen_memory.common.security.cryptography.key_provider import KeyRef


# ====================================================================== #
# 错误（F05 §错误语义：AuditIntegrityError 族）
# ====================================================================== #


class AuditIntegrityError(AgentMemoryError):
    """审计链损坏、验证失败或完整性装配不满足。"""


class AuditMigrationRequiredError(AuditIntegrityError):
    """启动时遇到无 proof 的历史审计库。

    服务无法证明历史数据在补签前未被改动，故不静默补签、不静默启动；显式离线
    迁移（备份、独占、记录「历史未受保护」边界）由实装 PR 按部署情况交付。
    """


class ChainConflictError(AuditIntegrityError):
    """链式追加的 CAS 冲突超过有界重试上限。

    冲突本身由完整性协调器重读链头并有限重试；超限抛本错误，不无限自旋。
    """


class AuditSchemaError(AuditIntegrityError):
    """审计完整性 schema/链头损坏或不一致。

    缺表、缺列、缺 head 行、head 与末事件不一致均归此类，启动期拒绝，不自动重建
    放行。
    """


class KeyCapabilityError(AuditIntegrityError):
    """审计密钥 capability 不足或历史验证材料不可用。"""


# ====================================================================== #
# 状态
# ====================================================================== #


class AuditIntegrityStatus(str, Enum):
    """结构化验证状态。``unsupported`` 与 ``incomplete`` 都不能当 ``clean``。"""

    UNSUPPORTED = "unsupported"  # 后端/部署没有所需能力
    CLEAN = "clean"  # 检查范围内证明一致；是否检查锚点另行标识
    TAMPERED = "tampered"  # 内容、proof、顺序或同位锚点不一致
    INCOMPLETE = "incomplete"  # 缺记录、未知格式/epoch、扫描未完成或证据不足
    ROLLBACK_SUSPECTED = "rollback_suspected"  # 本地落后于可信锚点或与已锚定历史冲突


# 公网入口的绝对资源边界。部署可用 ``AuditVerificationLimits`` 把有效上限调低，或在
# 硬上限内调高；不能只依赖 WorkloadGuard，因为它仅限制并发数，不限制单次扫描量。
DEFAULT_AUDIT_VERIFY_PAGE_SIZE = 1000
DEFAULT_AUDIT_VERIFY_MAX_SAMPLES = 20
HARD_MAX_AUDIT_VERIFY_PAGE_SIZE = 10_000
HARD_MAX_AUDIT_VERIFY_SAMPLES = 100


@dataclass(frozen=True)
class AuditVerificationLimits:
    """由服务端装配注入的单次验证资源上限，不接受请求 payload 覆盖。"""

    max_page_size: int = DEFAULT_AUDIT_VERIFY_PAGE_SIZE
    max_samples: int = DEFAULT_AUDIT_VERIFY_MAX_SAMPLES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_page_size, int)
            or isinstance(self.max_page_size, bool)
            or self.max_page_size <= 0
        ):
            raise ValueError("AuditVerificationLimits.max_page_size must be a positive integer")
        if (
            not isinstance(self.max_samples, int)
            or isinstance(self.max_samples, bool)
            or self.max_samples < 0
        ):
            raise ValueError("AuditVerificationLimits.max_samples must be a non-negative integer")
        for name, value, hard_max in (
            ("max_page_size", self.max_page_size, HARD_MAX_AUDIT_VERIFY_PAGE_SIZE),
            ("max_samples", self.max_samples, HARD_MAX_AUDIT_VERIFY_SAMPLES),
        ):
            if value > hard_max:
                raise ValueError(
                    f"AuditVerificationLimits.{name} cannot exceed hard limit {hard_max}"
                )


# ====================================================================== #
# Proof
# ====================================================================== #


@dataclass(frozen=True)
class Proof:
    """链式证明：独立于 ``AuditEvent.detail`` 的不可变结构。

    不写入 ``detail`` 是为了：调用方无法借 ``detail`` 注入 ``_hmac``；普通查询/过滤
    无意改写证明；证明字段与业务 detail 不混用；未来格式升级不破坏 ``AuditEvent``
    公共结构。``digest`` / ``previous_digest`` 为 hex 字符串，便于跨 TEXT 列稳定存储。
    """

    format_version: int  # 规范化与证明格式版本
    sequence: int  # 后端分配的单调序号
    previous_digest: str  # 前序证明摘要（hex）；首条为 GENESIS_DIGEST
    digest: str  # 当前事件证明（hex）
    key_id: str  # 不可逆 key 标识
    key_epoch: int  # 验证该事件所需的 key 代次

    def __post_init__(self) -> None:
        for name in ("format_version", "sequence", "key_epoch"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"Proof.{name} must be an integer")
            if value <= 0:
                raise ValueError(f"Proof.{name} must be positive")
        for name in ("previous_digest", "digest", "key_id"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"Proof.{name} must be a string")


# ====================================================================== #
# 验证结果
# ====================================================================== #


class AnchorStatus(str, Enum):
    """外部锚点核对状态；枚举值即稳定 wire 值。"""

    UNCHECKED = ""
    OK = "ok"
    LAGGING = "lagging"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AnchorState:
    """外部锚点核对结果。

    ``checked=False`` 时本结果**不得**声称防回滚--本地链只能检测内容修改与中间删除，
    不能独立证明数据库未被回滚到合法旧快照。
    """

    checked: bool  # 是否真的咨询了外部锚点
    status: AnchorStatus = AnchorStatus.UNCHECKED
    detail: str = ""  # 非敏感摘要

    def __post_init__(self) -> None:
        if not isinstance(self.checked, bool):
            raise TypeError("AnchorState.checked must be a bool")
        if not isinstance(self.status, AnchorStatus):
            raise TypeError("AnchorState.status must be AnchorStatus")
        if not isinstance(self.detail, str):
            raise TypeError("AnchorState.detail must be a string")
        if not self.checked and self.status is not AnchorStatus.UNCHECKED:
            raise ValueError("unchecked anchor must use AnchorStatus.UNCHECKED")
        if self.checked and self.status is AnchorStatus.UNCHECKED:
            raise ValueError("checked anchor must report a non-empty AnchorStatus")


@dataclass(frozen=True)
class AuditVerificationResult:
    """``verify_audit`` 的结构化、可序列化且不含秘密的返回。

    ``samples`` 有界（受请求 ``max_samples``、服务端 limits 与硬上限三者约束），不返回
    海量坏行索引造成内存 DoS。``truncated`` **只**表示还有错误样本因有效样本上限未返回；
    扫描缺页、序号缺口或无法到达稳定快照链头用 ``status=incomplete`` 表达，二者不混用。

    ``after_sequence > 0`` 时 checkpoint proof 是本次验证的一部分，计入 ``checked_count``；
    checkpoint 校验通过后即使没有后续记录，``high_water_mark`` 也等于 ``after_sequence``。
    它表示本次调用中**连续且成功验证到的最高 sequence**，不是调用开始或返回时的动态
    当前链头。只有验证恰好到达调用开始时稳定快照的 ``head.sequence`` 才可报告 clean；
    快照后的并发追加不影响本次 high-water mark。
    """

    status: AuditIntegrityStatus
    checked_count: int  # 实际校验的 proof 数（增量验证含 checkpoint proof）
    error_count: int  # 检出不一致的记录数
    truncated: bool  # 样本是否因上限截断
    high_water_mark: int  # 本次连续成功验证的最高 sequence；0 = 未验证任何记录
    key_epoch_range: tuple[int, int]  # (min, max) 见过的 key epoch
    anchor: AnchorState
    samples: tuple[Proof, ...] = field(default_factory=tuple)  # 有界 tampered 样本
    detail: str = ""  # 非敏感摘要

    def __post_init__(self) -> None:
        if not isinstance(self.status, AuditIntegrityStatus):
            raise TypeError("AuditVerificationResult.status must be AuditIntegrityStatus")
        for name in ("checked_count", "error_count", "high_water_mark"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"AuditVerificationResult.{name} must be an integer")
            if value < 0:
                raise ValueError(f"AuditVerificationResult.{name} must be non-negative")
        if not isinstance(self.truncated, bool):
            raise TypeError("AuditVerificationResult.truncated must be a bool")
        try:
            epoch_range = tuple(self.key_epoch_range)
        except TypeError as exc:
            raise TypeError(
                "AuditVerificationResult.key_epoch_range must contain two integers"
            ) from exc
        if len(epoch_range) != 2 or any(
            not isinstance(epoch, int) or isinstance(epoch, bool) for epoch in epoch_range
        ):
            raise TypeError("AuditVerificationResult.key_epoch_range must contain two integers")
        if epoch_range[0] < 0 or epoch_range[1] < epoch_range[0]:
            raise ValueError("AuditVerificationResult.key_epoch_range is invalid")
        try:
            samples = tuple(self.samples)
        except TypeError as exc:
            raise TypeError("AuditVerificationResult.samples must contain Proof values") from exc
        if not all(isinstance(proof, Proof) for proof in samples):
            raise TypeError("AuditVerificationResult.samples must contain Proof values")
        if len(samples) > HARD_MAX_AUDIT_VERIFY_SAMPLES:
            raise ValueError(
                "AuditVerificationResult.samples exceeds the absolute server-side limit"
            )
        if not isinstance(self.anchor, AnchorState):
            raise TypeError("AuditVerificationResult.anchor must be AnchorState")
        if not isinstance(self.detail, str):
            raise TypeError("AuditVerificationResult.detail must be a string")
        object.__setattr__(self, "key_epoch_range", epoch_range)
        object.__setattr__(self, "samples", samples)

    def to_body(self) -> dict[str, object]:
        """序列化为 dispatch Body（PR3 接口文档 §6.1 确认的对外契约）。

        纯 dict、无嵌套对象、无敏感字段；供真实认证接入后的 HTTP 及未来 MCP / CLI
        一等入口复用，避免各 surface 各写一份漂移。当前接口先行阶段尚未注册这些
        surface 入口。字段名一经发布即为线上契约，变更需评审。
        """
        return {
            "op": "verify_audit",
            "status": self.status.value,
            "checked_count": self.checked_count,
            "error_count": self.error_count,
            "truncated": self.truncated,
            "high_water_mark": self.high_water_mark,
            "key_epoch_range": [self.key_epoch_range[0], self.key_epoch_range[1]],
            "anchor": {
                "checked": self.anchor.checked,
                "status": self.anchor.status.value,
                "detail": self.anchor.detail,
            },
            "samples": [
                {
                    "sequence": proof.sequence,
                    "format_version": proof.format_version,
                    "previous_digest": proof.previous_digest,
                    "digest": proof.digest,
                    "key_id": proof.key_id,
                    "key_epoch": proof.key_epoch,
                }
                for proof in self.samples
            ],
            "detail": self.detail,
        }


# ====================================================================== #
# Producer / Provider
# ====================================================================== #


class AuditIntegrityProducer(Factory):
    """AuditIntegrityProvider 的注册式工厂（与契约同处接口层）。

    各实现在 ``audit_integrity_impl`` 下以 ``@AuditIntegrityProducer.register("<名>")``
    自注册；配置顶层段为 ``audit_integrity``，其具名实例引用独立的 ``key_provider``
    与 ``audit`` 具名实例（实装 PR 落地装配链）。
    """

    TOP_NAME = "audit_integrity"


class AuditIntegrityProvider(ABC):
    """审计事件密码学完整性能力。"""

    @abstractmethod
    def capabilities(self) -> ChainStoreCapability:
        """本 provider 背后 ChainStore 的能力声明（透传自后端）。"""

    @abstractmethod
    def chain_store(self) -> ChainedAuditStore:
        """返回 provider 实际写入和验证的 chain store 实例。

        装配层与 :class:`ProtectedAuditLogger` 用对象 identity 校验它就是查询所用的
        ``AuditLogger``，不能只靠具名配置或注释假定两者指向同一后端。
        """

    @abstractmethod
    def record_chained(self, event: AuditEvent) -> ChainedRecord:
        """规范化事件、计算 proof、原子追加到链（有界 CAS 重试）。

        返回写入的带 proof 记录。关键事件写入失败时抛
        :class:`AuditIntegrityError`，由调用方按事件等级执行 fail-closed。
        """

    @abstractmethod
    def verify(
        self,
        *,
        after_sequence: int = 0,
        page_size: int = DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
        max_samples: int = DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
        anchor_policy: str = "if_configured",
    ) -> AuditVerificationResult:
        """流式验证指定稳定快照。

        输入只允许服务端验证参数（范围、页预算、anchor policy），不接受调用方传入的
        expected digest / key / proof。全量验证在 ``WorkloadGuard`` 独立预算下执行。
        ``anchor_policy``：``if_configured``（默认）、``required``、``skip``。

        ``after_sequence > 0`` 时实现必须从 ``ChainedAuditStore.read_stable_snapshot``
        取得并验证该序号的 checkpoint proof，不能从 genesis 盲接，也不能信任调用方
        提供的历史 digest。分页扫描固定在该快照的 ``head.sequence``，因此验证期间的
        并发追加不进入本次结果。返回样本数不得超过传入 ``max_samples``；PEP 还会按
        服务端 limits 做二次约束。
        """

    @abstractmethod
    def active_key_ref(self) -> KeyRef:
        """当前用于签发新证明的活动 key 标识。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康返回 ``None``，否则抛异常（含后端与 key 检查）。"""

    def is_test_only(self) -> bool:
        """是否仅测试实现（如临时链）。生产 Runtime 拒绝。"""
        return False
