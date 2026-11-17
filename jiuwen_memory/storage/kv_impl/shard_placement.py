# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""分片归属记录与拓扑登记的数据形态（F09 D7 / D13，运维见 deploy/sharding/README.md）。

归属记录是「记忆在哪个分片」的唯一事实来源：

.. code-block:: text

    归属 key（scope 内）：/__shard_placement/<unit_id>
    值：{"shard": "working", "status": "active",
         "token": "<uuid4>", "created_at": <存储侧 Unix 秒>}

状态：``pending``（插入进行中）、``active``（已生效）、``cleaning``（删除 /
对账进行中）、``moving``（F09 D16 搬迁进行中）。**操作 token** 只存在于归属记录里，
用于按整条值比较，识别「这条归属是不是我创建的」；创建时间取存储侧时间，只用于
超期判断。

搬迁相关的可选字段只在开启 D16 且确有内容时序列化：``relocate_to``（待搬迁 /
搬迁目标）、``step``（搬迁进度）、``cleanup_from``（已切换、源分片待清理）。空值不
写入，因此未开启搬迁时归属记录的字节与旧版本完全一致。``failures`` 记录连续搬迁失败次数，
超过上限后清除标记，避免永久失败的记忆每轮重试。``mark_digest`` 是打标记时那次写入的
正文摘要，执行搬迁时与源分片当前数据比对，不一致就按当前真源重新裁决目标。``claim`` /
``claimed_at`` 是清理源分片的独占执行权：持有期间只有持有者能清除 ``cleanup_from``，
同 id 的 delete 返回可重试错误，新的搬迁不能开始。

拓扑登记记录每个分片的物理标识与状态（``serving`` / ``retiring``），也放在
归属存储的保留前缀下，避免成为可被绕过的业务端口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope

from ..shard_routing import SHARD_PLACEMENT_KEY_PREFIX

#: 拓扑登记使用的控制 Scope（归属存储内不带业务 scope 维度）。
CONTROL_SCOPE = Scope()


class PlacementStatus(str, Enum):
    """归属记录状态（对外不可见，仅 storage 内部使用）。"""

    PENDING = "pending"
    ACTIVE = "active"
    CLEANING = "cleaning"
    MOVING = "moving"


class TopologyStatus(str, Enum):
    """分片在拓扑登记中的状态。"""

    SERVING = "serving"
    RETIRING = "retiring"


@dataclass(frozen=True)
class PlacementRecord:
    """一条归属记录（可选字段为空时不序列化，保持旧字节兼容）。"""

    shard: str
    status: PlacementStatus
    token: str
    created_at: float
    relocate_to: str | None = None
    step: str | None = None
    cleanup_from: str | None = None
    failures: int | None = None
    mark_digest: str | None = None
    claim: str | None = None
    claimed_at: float | None = None

    def to_bytes(self) -> bytes:
        payload: dict[str, object] = {
            "shard": self.shard,
            "status": self.status.value,
            "token": self.token,
            "created_at": self.created_at,
        }
        if self.relocate_to is not None:
            payload["relocate_to"] = self.relocate_to
        if self.step is not None:
            payload["step"] = self.step
        if self.cleanup_from is not None:
            payload["cleanup_from"] = self.cleanup_from
        if self.failures is not None:
            payload["failures"] = self.failures
        if self.mark_digest is not None:
            payload["mark_digest"] = self.mark_digest
        if self.claim is not None:
            payload["claim"] = self.claim
        if self.claimed_at is not None:
            payload["claimed_at"] = self.claimed_at
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> PlacementRecord:
        """解码归属记录；结构非法时抛 ``ValidationError``（调用方按未定位处理）。"""
        try:
            payload = json.loads(raw.decode("utf-8"))
            return cls(
                shard=str(payload["shard"]),
                status=PlacementStatus(payload["status"]),
                token=str(payload["token"]),
                created_at=float(payload["created_at"]),
                relocate_to=_optional_str(payload.get("relocate_to")),
                step=_optional_str(payload.get("step")),
                cleanup_from=_optional_str(payload.get("cleanup_from")),
                failures=_optional_int(payload.get("failures")),
                mark_digest=_optional_str(payload.get("mark_digest")),
                claim=_optional_str(payload.get("claim")),
                claimed_at=_optional_float(payload.get("claimed_at")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"corrupt shard placement record: {exc}") from exc


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


@dataclass(frozen=True)
class TopologyEntry:
    """拓扑登记中的一条分片记录。"""

    shard: str
    ports: dict[str, str]
    status: TopologyStatus = TopologyStatus.SERVING

    def to_bytes(self) -> bytes:
        payload = {
            "ports": dict(sorted(self.ports.items())),
            "status": self.status.value,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> TopologyEntry:
        try:
            payload = json.loads(raw.decode("utf-8"))
            return cls(
                shard="",
                ports={str(k): str(v) for k, v in dict(payload["ports"]).items()},
                status=TopologyStatus(payload["status"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"corrupt shard topology record: {exc}") from exc


def placement_key(unit_id: str) -> str:
    """归属记录的 key（同一 scope 内按 unit_id 唯一）。"""
    return f"{SHARD_PLACEMENT_KEY_PREFIX}{unit_id}"


def unit_id_from_placement_key(key: str) -> str | None:
    """从归属 key 反解 unit_id；不是归属 key 时返回 ``None``。"""
    token = SHARD_PLACEMENT_KEY_PREFIX
    return key[len(token):] if key.startswith(token) else None


def topology_key(shard: str) -> str:
    """拓扑登记的 key。"""
    return f"{SHARD_PLACEMENT_KEY_PREFIX}topology/{shard}"


#: 待搬迁索引的子前缀（F09 D16）：只为有搬迁工作的记忆各存一条，``relocate_pending``
#: 扫描它而不是全部归属记录。
RELOCATION_INDEX_PREFIX = f"{SHARD_PLACEMENT_KEY_PREFIX}relocation/"


def relocation_index_key(unit_id: str) -> str:
    """待搬迁索引的 key（值不承载语义，权威状态始终在归属记录里）。"""
    return f"{RELOCATION_INDEX_PREFIX}{unit_id}"


def is_control_suffix(suffix: str) -> bool:
    """归属前缀下不是某条记忆归属的控制条目（拓扑登记、待搬迁索引）。"""
    return suffix.startswith("topology/") or suffix.startswith("relocation/")
