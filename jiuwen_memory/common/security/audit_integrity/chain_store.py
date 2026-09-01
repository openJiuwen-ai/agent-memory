# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ChainedAuditStore - 后端原子链式追加 capability（F05 §Audit Integrity）。

接口**表达行为保证**，而不是只提供若干方法：``ChainStoreCapability`` 声明持久化、
原子追加、稳定链头快照、key epoch、外部锚点、流式扫描六项能力。完整性能力不根据
target 名判断后端性质；必需 capability 缺失时装配拒绝（F05 §依据 capability 做安全
决策）。

核心操作语义::

    read_head() -> ChainHead
    append(record, expected_head) -> ChainHead
    read_stable_snapshot(after_sequence) -> ChainSnapshot
    scan(after_sequence, limit, through_sequence=...) -> page
    health() -> None

``append`` 必须把「比较 expected head、插入事件+proof、推进 head」放进同一临界区/事务。
CAS 冲突由完整性协调器（``AuditIntegrityProvider``）重新读取链头并有界重试；超限抛
:class:`~jiuwen_memory.common.security.audit_integrity.base.ChainConflictError`，不无限自旋。

**接口先行说明**：本文件只固定契约。内存 / SQLite 审计后端叠加实现本 capability
（以及锚点的具体产品实现）随实装 PR 合入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from jiuwen_memory.common.type_def import AuditEvent

from .base import Proof

# ====================================================================== #
# 链头 / 快照 / 记录
# ====================================================================== #


# 首条事件之前的前序摘要：固定全零 hex（SHA-256 摘要的 64 hex 字符）。链头 sequence=0
# 时 digest 即此值。选全零而非随机常量，是为了让「空链」与「非空链首条」的前序摘要
# 在视觉与逻辑上都可区分于任何真实 HMAC 输出。
GENESIS_DIGEST = "0" * 64


@dataclass(frozen=True)
class ChainHead:
    """链头游标：最后一条已追加记录的摘要与 key ref。

    ``sequence=0`` 表示空链（仅有 genesis head）。``format_version`` 是写入该 head 时
    使用的规范化格式版本，跨重启续链时据此识别不兼容的旧链。
    """

    sequence: int  # 末事件序号；0 = 空链
    digest: str  # 末事件 proof 摘要（hex）；空链为 GENESIS_DIGEST
    key_id: str  # 末事件使用的 key id；空链为 ""
    key_epoch: int  # 末事件使用的 key epoch；空链为 0
    format_version: int  # 写入该 head 时的规范化格式版本


@dataclass(frozen=True)
class ChainedRecord:
    """一条带 proof 的审计记录。

    普通 ``AuditLogger.query()`` 仍返回 :class:`~jiuwen_memory.common.type_def.AuditEvent`；
    完整性扫描经 :meth:`ChainedAuditStore.scan` 返回本类型，proof 与事件分离存储。
    """

    event: AuditEvent
    proof: Proof


@dataclass(frozen=True)
class ChainSnapshot:
    """增量验证 checkpoint、链头与末事件的一致快照。

    ``after_sequence=0`` 表示从 genesis 开始，``checkpoint`` 必须为 ``None``；大于 0
    时 ``checkpoint`` 是**恰好**第 ``after_sequence`` 条记录，provider 先验证其 proof
    再把 digest 用作续链基线。不存在时返回 ``None``，provider 必须报告 ``incomplete``，
    不得回落 genesis 或跳到下一条。

    checkpoint、链头和末事件必须在同一临界区/事务中读取，否则并发追加、截断或
    「head 已推进但事件未落盘」等故障注入会让验证窗口失去确定边界。
    """

    head: ChainHead
    last_record: ChainedRecord | None  # 空链为 None
    after_sequence: int = 0
    checkpoint: ChainedRecord | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.after_sequence, int)
            or isinstance(self.after_sequence, bool)
            or self.after_sequence < 0
        ):
            raise ValueError("ChainSnapshot.after_sequence must be a non-negative integer")
        if self.after_sequence == 0 and self.checkpoint is not None:
            raise ValueError("genesis snapshot cannot carry a checkpoint record")
        if (
            self.checkpoint is not None
            and self.checkpoint.proof.sequence != self.after_sequence
        ):
            raise ValueError("checkpoint sequence must equal ChainSnapshot.after_sequence")


# ====================================================================== #
# Capability
# ====================================================================== #


@dataclass(frozen=True)
class ChainStoreCapability:
    """后端显式声明的行为保证。

    装配期据此判断后端能否承担完整性保护，不从 target 名或类名推断（F05 §依据
    capability 做安全决策）。任一必需 capability 缺失即拒绝装配。
    """

    persistent: bool  # 是否跨进程持久化
    atomic_append: bool  # 是否支持 expected-head CAS / 等价事务
    stable_head_snapshot: bool  # 能否一致读取链头与末事件
    key_epoch: bool  # 是否持久化 key id / epoch
    external_anchor: bool  # 是否能与独立锚点交互
    streaming_scan: bool  # 是否支持有界 keyset / 游标扫描

    def __post_init__(self) -> None:
        for name in (
            "persistent",
            "atomic_append",
            "stable_head_snapshot",
            "key_epoch",
            "external_anchor",
            "streaming_scan",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"ChainStoreCapability.{name} must be a bool")


# ====================================================================== #
# ChainedAuditStore
# ====================================================================== #


class ChainedAuditStore(ABC):
    """后端原子链式追加 capability。

    具体审计后端（内存 / SQLite）**同时实现** :class:`~jiuwen_memory.common.audit.base.AuditLogger`
    与本接口：普通记录/查询走 AuditLogger，链式追加/扫描走本接口。装配时要求用于完整
    性保护的事件 logger 与 chain store 是**同一具名实例**，防止「查询一个库、签另一个库」。
    """

    @abstractmethod
    def capabilities(self) -> ChainStoreCapability:
        """本后端的能力声明。装配期与运行期皆可读取。"""

    @abstractmethod
    def read_head(self) -> ChainHead:
        """读取当前链头。空链返回 ``sequence=0`` 的 genesis head。"""

    @abstractmethod
    def append(self, record: ChainedRecord, expected_head: ChainHead) -> ChainHead:
        """原子比较链头并追加。

        把「比较 ``expected_head`` 与当前 head、插入事件+proof、推进 head」放进同一
        临界区/事务。``expected_head`` 与当前 head 不一致抛
        :class:`~jiuwen_memory.common.security.audit_integrity.base.ChainConflictError`，
        由协调器重读并有界重试。成功返回推进后的新 head。
        """

    @abstractmethod
    def read_stable_snapshot(self, after_sequence: int = 0) -> ChainSnapshot:
        """一致读取增量 checkpoint、链头与末事件。

        返回时 ``head.sequence`` 是本次验证的固定扫描上界。合法并发写入只能追加，故
        快照后新增的更大 sequence 留给下一次验证；记录更新/删除/截断不属于合法并发，
        实现不得用重新取 head 或静默缩短扫描范围掩盖它。
        """

    @abstractmethod
    def scan(
        self,
        after_sequence: int,
        limit: int,
        *,
        through_sequence: int,
    ) -> list[ChainedRecord]:
        """有界 keyset 扫描固定窗口内的下一页记录。

        只返回 ``after_sequence < sequence <= through_sequence``，按 ``sequence`` 升序，
        以 keyset/游标推进，**不得**用随偏移增长的 ``OFFSET``（大链上退化为全表扫描）。
        ``through_sequence`` 必须取自同次 ``read_stable_snapshot`` 的 ``head.sequence``，
        不能每页重读当前 head；这样并发追加不会让一次验证永远追赶移动链头。

        ``limit`` 同时受实现硬上限和服务端 ``AuditVerificationLimits`` 约束。页为空、
        首条不是期望的下一 sequence、页内有缺口，且尚未到 ``through_sequence`` 时，
        provider 必须返回 ``incomplete``；不得把截断前缀报告为 clean。
        """

    @abstractmethod
    def health(self) -> None:
        """存活与 schema 一致性探测。

        严格校验 schema 版本、核心列、head 行与末事件一致性；损坏即抛
        :class:`~jiuwen_memory.common.security.audit_integrity.base.AuditSchemaError`，
        不自动重建放行。
        """


# ====================================================================== #
# 外部可信锚点
# ====================================================================== #


@dataclass(frozen=True)
class AnchorRecord:
    """锚点记录的最小结构。

    具体产品实现不在本 PR；本结构供 ``AuditAnchor`` 实现与 conformance/攻击测试使用。
    """

    chain_id: str
    sequence: int
    digest: str  # hex
    key_id: str
    epoch: int
    format_version: int
    anchored_at: str  # ISO8601；由锚点侧填


class AuditAnchor(ABC):
    """外部可信锚点接口。

    本地链式完整性只能检测内容修改与中间删除，**不能**独立证明数据库未被回滚到合法
    旧快照。需要防尾删与回滚的部署周期性把链头写入独立可信锚点。具体产品（WORM、云
    审计、KMS/Vault 附加日志）实现不在本 PR；测试用 fake anchor 只进 conformance/攻击
    测试，不注册成生产默认 target。
    """

    @abstractmethod
    def anchor_head(self, head: ChainHead, *, chain_id: str) -> AnchorRecord:
        """把当前链头锚定到外部可信存储，返回锚点记录。"""

    @abstractmethod
    def read_anchored(self, *, chain_id: str) -> AnchorRecord | None:
        """读取已锚定的最新链头；未锚定过返回 ``None``。"""

    @abstractmethod
    def health(self) -> None:
        """锚点可用性探测。"""
