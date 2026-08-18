"""``MemoryUnit`` ↔ bytes 编解码（无状态，跨层共用）。

真源不是「一个存 MemoryUnit 的模块」——它就是裸 :class:`~storage.kv.KVStore` 存
**字节**（key 带前缀：建索引记忆 ``/memory/{id}``、未建索引的 infer 原文
``/messages/{id}``，见 :mod:`common.type_def.memory` / :mod:`common.type_def.raw`；
scope 做命名空间隔离）。``MemoryUnit`` 对象只在两处出现：写入时把刚产出的单元
:func:`dumps` 成字节落盘；产出结果时（get/recall/list/inspect）从字节 :func:`loads`
回对象。系统其余流转只搬 id 与字节，不长期持有 ``MemoryUnit``。

编解码与 ``MemoryUnit`` 同住 ``common.type_def``：它只依赖结构定义、不依赖任何
存储后端，调用方（control/retrieval/接入 surface）按需引用这对纯函数。
"""

from __future__ import annotations

import json
from datetime import datetime

from .memory import (
    ContentLayers,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Segment,
    Temporal,
)
from .scope import Scope

# 正排 JSON schema 版本：写入侧固定写出，读取侧据此分流破坏性结构变更。
# 「加字段」是兼容演进（靠下方 loads 缺省取默认消化，不升版本）；改字段
# 含义/结构才升版本并在 loads 里按 _v 分支。
# _v=2：内容侧由扁平 content/assets/source 改为 segments 列表（破坏性结构变更）；
#       loads 对 _v<2 的老数据把单一 content/assets/source 读成单元素 segments。
# _v=4：metadata 破坏性拆分为 system_metadata / user_metadata。不在运行时
#       猜测旧混合字段的归属；旧数据必须先显式迁移。
_V = 4


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _pt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def dumps(unit: MemoryUnit) -> bytes:
    """``MemoryUnit`` → JSON 字节（带 ``_v`` 版本号、枚举取 value、时间取 isoformat）。"""
    return json.dumps(
        {
            "_v": _V,
            "id": unit.id,
            "scope": [
                unit.scope.org,
                unit.scope.space,
                unit.scope.user,
                unit.scope.agent,
                unit.scope.session,
            ],
            "tier": unit.tier.value,
            "layers": {
                "l0": unit.layers.l0,
                "l1": unit.layers.l1,
            },
            "segments": [
                {"content": s.content, "assets": list(s.assets), "source": s.source.value}
                for s in unit.segments
            ],
            "source_ref": unit.source_ref,
            "temporal": [
                _dt(unit.temporal.t_event),
                _dt(unit.temporal.t_ingest),
                _dt(unit.temporal.t_valid),
                _dt(unit.temporal.t_invalid),
                _dt(unit.temporal.t_message),
            ],
            "provenance": list(unit.provenance),
            "supersedes": unit.supersedes,
            "tags": list(unit.tags),
            "system_metadata": dict(unit.system_metadata),
            "user_metadata": dict(unit.user_metadata),
            "lifecycle": unit.lifecycle.value,
            "entities": list(unit.entities),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def loads(raw: bytes) -> MemoryUnit | None:
    """JSON 字节 → ``MemoryUnit``（逆 :func:`dumps`）。

    **容错演进**：未知字段忽略、缺失字段取默认——给 ``MemoryUnit`` 增字段时
    老数据可无迁移读出。``_v`` 缺省视为 1（首版无版本号的历史数据）；将来出现
    破坏性结构变更时在此按 ``_v`` 分支。

    **非 MemoryUnit 容错**：KVStore 中除 MemoryUnit 外还有索引/跟踪等
    内部记录（value 为 list 等非 dict JSON）。碰到非 dict 时返回 ``None``，
    让调用方用 ``[u for u in ... if u is not None]`` 过滤，无需靠 key
    前缀猜测哪些是 MemoryUnit。
    """
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        return None
    if "id" not in payload:
        return None
    version = payload.get("_v", 1)  # 据此分流破坏性变更
    if version < 4:
        raise ValueError(
            "MemoryUnit codec version < 4 uses mixed metadata; run the explicit metadata "
            "migration before loading it"
        )
    raw_scope = list(payload.get("scope") or [])
    padded_scope = (raw_scope + ["", "", "", "", ""])[:5]
    tm = (list(payload.get("temporal") or []) + [None, None, None, None, None])[:5]
    if version >= 2:
        segments = []
        for segment_payload in payload.get("segments") or []:
            segments.append(
                Segment(
                    content=segment_payload.get("content", ""),
                    assets=list(segment_payload.get("assets") or []),
                    source=Modality(
                        segment_payload.get("source", Modality.TEXT.value)
                    ),
                )
            )
    else:
        # _v<2 老数据：扁平 content/assets/source → 单元素 segments（无迁移读出）。
        segments = [
            Segment(
                content=payload.get("content", ""),
                assets=list(payload.get("assets") or []),
                source=Modality(payload.get("source", Modality.TEXT.value)),
            )
        ]
    return MemoryUnit(
        id=payload.get("id", ""),
        scope=(
            Scope(
                org=padded_scope[0],
                space=padded_scope[1],
                user=padded_scope[2],
                agent=padded_scope[3],
                session=padded_scope[4],
            )
            if version >= 3 or len(raw_scope) >= 5
            else Scope(
                org=padded_scope[0],
                user=padded_scope[1],
                agent=padded_scope[2],
                session=padded_scope[3],
            )
        ),
        tier=MemoryTier(payload.get("tier", MemoryTier.EPISODIC.value)),
        layers=ContentLayers(
            l0=str((payload.get("layers") or {}).get("l0", "") or ""),
            l1=str((payload.get("layers") or {}).get("l1", "") or ""),
        ),
        segments=segments,
        source_ref=payload.get("source_ref", ""),
        temporal=Temporal(
            t_event=_pt(tm[0]),
            t_ingest=_pt(tm[1]),
            t_valid=_pt(tm[2]),
            t_invalid=_pt(tm[3]),
            t_message=_pt(tm[4]) if len(tm) > 4 else None,
        ),
        provenance=list(payload.get("provenance") or []),
        supersedes=payload.get("supersedes", ""),
        tags=list(payload.get("tags") or []),
        system_metadata=dict(payload.get("system_metadata") or {}),
        user_metadata=dict(payload.get("user_metadata") or {}),
        lifecycle=LifecycleState(payload.get("lifecycle", LifecycleState.ACTIVE.value)),
        entities=list(payload.get("entities") or []),
    )
