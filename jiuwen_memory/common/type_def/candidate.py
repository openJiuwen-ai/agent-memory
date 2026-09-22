# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""演进候选源的数据契约（F03 dreaming）。

evolve 的候选不再固定为「list scope 全量」——候选从哪来、怎么筛，由
``CandidateSource`` 声明（谓词 / 点名 / 召回 / 扇出四型），由 control 层
resolver 消费产出 :class:`CandidateOutcome`。本模块只有**数据**：

- 纯 dataclass + 原生字段，可经 :func:`candidate_to_dict` /
  :func:`candidate_from_dict` 与 dict DSL 互转——注册态 dreaming 任务把它随
  interval 持久化到 KV，重启恢复后原样重建 resolver（候选源必须是数据不是
  代码，F04 D3）；
- 时间窗 ``window``（秒）不落盘绝对时间戳——resolver 在**每次 resolve 时**
  现算 ``t_ingest GTE now-window`` 下推，重启后窗口随 tick 自然滑动。

分派是**注册表**不是 isinstance 阶梯：每型候选源持有 ``kind`` 判别键
（即 dict DSL 的 ``"type"`` 值），序列化按 ``_CANDIDATE_CODECS`` 查表、
control 层 resolver 装配按同键查表——新增候选源类型 = 新 dataclass（含
kind）+ 两侧各一条注册表条目，无分支表要改（与 ``EvolverProducer`` 同款
注册表风格，S09 §1/§9 基线一致）。

filters 的契约：谓词源为本体、点名源恒无（id 列表即完整答案）、召回源为
收窄用（与 query 正交）、枚举源嵌套在 child（F04 D3）——不为形式统一给
每源塞 filters。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, NamedTuple

from jiuwen_memory.common.errors import ValidationError

from .filter import FilterExpr, normalize, to_dict
from .memory import MemoryUnit
from .scope import Scope

MAX_EXPLICIT_CANDIDATE_IDS = 10_000
MAX_RECALL_TOP_K = 10_000


@dataclass
class PredicateCandidate:
    """① 谓词源：``list_units`` + filters 下推，恒单桶。

    ``window``（秒）在 resolver 内现算 ``t_ingest GTE cutoff`（毫秒）合并
    进 filters 下推（F04 D5：t_ingest 恒非空，内核接入路径强制
    盖章）；``None`` 表示不限时间（全量型）。
    """

    kind: ClassVar[str] = "predicate"  # 判别键：dict DSL "type" 值 + resolver 工厂查表键

    filters: FilterExpr | None = None
    window: int | None = None  # None=不限时间（全量型）；cutoff 每 tick 现算

    def __post_init__(self) -> None:
        if self.window is not None and (
            not isinstance(self.window, int)
            or isinstance(self.window, bool)
            or self.window <= 0
        ):
            raise ValidationError(f"predicate.window 必须是正整数秒：{self.window!r}")
        self.filters = normalize(self.filters)


@dataclass
class IdsCandidate:
    """② 点名源：``load_units`` 点读，越权 id 自然缺失（差集回显）。"""

    kind: ClassVar[str] = "ids"

    unit_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.unit_ids, list) or not all(
            isinstance(item, str) and item for item in self.unit_ids
        ):
            raise ValidationError("ids.unit_ids 必须是非空字符串列表")
        if not self.unit_ids:
            raise ValidationError("ids 候选源的 unit_ids 不能为空（点名源即完整答案）")
        if len(self.unit_ids) > MAX_EXPLICIT_CANDIDATE_IDS:
            raise ValidationError(
                "ids.unit_ids 超过单次上限 "
                f"{MAX_EXPLICIT_CANDIDATE_IDS}：{len(self.unit_ids)}"
            )
        self.unit_ids = list(self.unit_ids)


@dataclass
class RecallCandidate:
    """③ 召回源：语义召回选批（每 tick 重新召回——top-k 是「当前时刻」的答案）。

    ``query`` 为检索请求原语（目前取 ``{"text": ...}``）；``filters`` 召回后
    谓词收窄，与 query 正交（query 管「像什么」，filters 管「还须满足什么」）。
    """

    kind: ClassVar[str] = "recall"

    query: dict[str, Any] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)  # 复用 RecallChannel 取值
    top_k: int = 50
    filters: FilterExpr | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, dict):
            raise ValidationError(f"recall.query 必须是对象：{self.query!r}")
        if not isinstance(self.channels, list) or not all(
            isinstance(channel, str) for channel in self.channels
        ):
            raise ValidationError(f"recall.channels 必须是字符串列表：{self.channels!r}")
        if (
            not isinstance(self.top_k, int)
            or isinstance(self.top_k, bool)
            or self.top_k <= 0
        ):
            raise ValidationError(f"recall.top_k 必须是正整数：{self.top_k!r}")
        if self.top_k > MAX_RECALL_TOP_K:
            raise ValidationError(
                f"recall.top_k 超过单次上限 {MAX_RECALL_TOP_K}：{self.top_k}"
            )
        self.query = dict(self.query)
        self.channels = list(self.channels)
        self.filters = normalize(self.filters)


# 形状约束的合法字段域：org 之外四维（org 是租户标识，由父 scope 前缀约束；
# 「org 为空」的桶不属于任何租户，无业务语义）。
_SHAPE_FIELDS: frozenset[str] = frozenset({"space", "user", "agent", "session"})


@dataclass
class FanOutCandidate:
    """④ 枚举源：``scopes()`` × 子谓词，多桶（演进按桶进行）。

    形状约束（F04 D3）：父子 scope 前缀匹配只表达「覆盖范围」，表达
    不了「桶形状」——如「所有用户桶」需在前缀 ``org=acme`` 之外再要求
    agent/session 为空。``require_empty`` / ``require_nonempty`` 指定子桶
    scope 字段的空/非空硬约束，缺省空集 = 不约束（现行为）；只会从覆盖集
    **剔除**子桶、永不放大（安全单向收窄，无越权面）。字段域与两集合交集
    在 ``__post_init__`` fail-closed 校验——所有构造路径（Python API /
    from_dict）共享同一校验，非法数据不可能进入注册表持久层。
    """

    kind: ClassVar[str] = "fan_out"

    child: PredicateCandidate = field(default_factory=PredicateCandidate)
    require_empty: frozenset[str] = frozenset()
    require_nonempty: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.child, PredicateCandidate):
            raise ValidationError(
                "fan_out.child 只能是 PredicateCandidate，"
                f"收到 {type(self.child).__name__}"
            )
        bad = (self.require_empty | self.require_nonempty) - _SHAPE_FIELDS
        if bad:
            raise ValidationError(
                f"fan_out 形状约束只允许 scope 字段 {sorted(_SHAPE_FIELDS)}，"
                f"收到 {sorted(bad)}"
            )
        both = self.require_empty & self.require_nonempty
        if both:
            raise ValidationError(
                f"同一 scope 字段不能同时要求为空与非空：{sorted(both)}"
            )


# 判别联合：四型候选源的统一承载（消费方按 kind 键查注册表分派，无 isinstance 阶梯）。
CandidateSource = PredicateCandidate | IdsCandidate | RecallCandidate | FanOutCandidate


@dataclass
class CandidateGroup:
    """一个候选桶：演进按桶进行（consolidate 需桶内上下文，不跨用户混桶）。"""

    scope: Scope
    units: list[MemoryUnit] = field(default_factory=list)


@dataclass
class CandidateOutcome:
    """resolver 的统一产出：①②③恒单桶；④多桶。

    ``requested_ids`` / ``loaded_ids`` / ``skipped`` 仅②点名源填——差集回显
    （用户能看到谁被漏了、为什么）；skipped 形如 ``["m3:not_found"]``。
    ``denied_scopes`` 仅④枚举源逐桶授权被拒时填——结构化回显被拒桶
    （不静默吞、不中断整批），其余源恒 None。
    """

    groups: list[CandidateGroup] = field(default_factory=list)
    requested_ids: list[str] | None = None
    loaded_ids: list[str] | None = None
    skipped: list[str] | None = None
    denied_scopes: list[str] | None = None


# ---------------------------------------------------------------------------
# dict DSL 序列化（HTTP/SDK 边界解析 + KV 持久化 round-trip）
# ---------------------------------------------------------------------------


class _CandidateCodec(NamedTuple):
    """单型候选源的 dict DSL 编解码（注册表条目——新增类型只加条目，无分支）。"""

    to_dict: Callable[[Any], dict]
    from_dict: Callable[[dict], CandidateSource]


def _predicate_to_dict(source: PredicateCandidate) -> dict:
    return {"filters": to_dict(source.filters), "window": source.window}


def _predicate_from_dict(data: dict) -> PredicateCandidate:
    return PredicateCandidate(
        filters=normalize(data.get("filters")),
        window=data.get("window"),
    )


def _ids_to_dict(source: IdsCandidate) -> dict:
    return {"unit_ids": list(source.unit_ids)}


def _ids_from_dict(data: dict) -> IdsCandidate:
    unit_ids = data.get("unit_ids")
    if not isinstance(unit_ids, list) or not all(isinstance(i, str) for i in unit_ids):
        raise ValidationError(
            f"ids 候选源的 unit_ids 必须是字符串列表：{unit_ids!r}"
        )
    return IdsCandidate(unit_ids=list(unit_ids))


def _recall_to_dict(source: RecallCandidate) -> dict:
    return {
        "query": dict(source.query),
        "channels": list(source.channels),
        "top_k": source.top_k,
        "filters": to_dict(source.filters),
    }


def _recall_from_dict(data: dict) -> RecallCandidate:
    query = data.get("query")
    if not isinstance(query, dict):
        raise ValidationError(f"recall 候选源的 query 必须是对象：{query!r}")
    channels = data.get("channels", [])
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        raise ValidationError(
            f"recall 候选源的 channels 必须是字符串列表：{channels!r}"
        )
    top_k = data.get("top_k", 50)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValidationError(f"recall 候选源的 top_k 必须是正整数：{top_k!r}")
    return RecallCandidate(
        query=dict(query),
        channels=list(channels),
        top_k=top_k,
        filters=normalize(data.get("filters")),
    )


def _fan_out_to_dict(source: FanOutCandidate) -> dict:
    return {
        "child": candidate_to_dict(source.child),
        # frozenset → sorted list（确定性序列化，round-trip 无损）。
        "require_empty": sorted(source.require_empty),
        "require_nonempty": sorted(source.require_nonempty),
    }


def _fan_out_from_dict(data: dict) -> FanOutCandidate:
    child = data.get("child")
    if not isinstance(child, dict):
        raise ValidationError(f"fan_out 候选源的 child 必须是谓词源对象：{child!r}")
    child_type = child.get("type", "predicate")
    if child_type != "predicate":
        raise ValidationError(
            "fan_out 候选源的 child.type 只能是 'predicate'："
            f"{child_type!r}"
        )
    return FanOutCandidate(
        child=candidate_from_dict({**child, "type": "predicate"}),
        require_empty=_parse_shape_set(data.get("require_empty"), "require_empty"),
        require_nonempty=_parse_shape_set(data.get("require_nonempty"), "require_nonempty"),
    )


# kind → 编解码注册表：candidate_to_dict / candidate_from_dict 共用同一判别键
# （dict DSL 的 "type" 值），新增候选源类型 = dataclass 加 kind + 此处一条条目。
_CANDIDATE_CODECS: dict[str, _CandidateCodec] = {
    PredicateCandidate.kind: _CandidateCodec(_predicate_to_dict, _predicate_from_dict),
    IdsCandidate.kind: _CandidateCodec(_ids_to_dict, _ids_from_dict),
    RecallCandidate.kind: _CandidateCodec(_recall_to_dict, _recall_from_dict),
    FanOutCandidate.kind: _CandidateCodec(_fan_out_to_dict, _fan_out_from_dict),
}


def register_candidate_codec(
    kind: str,
    to_dict: Callable[[Any], dict],
    from_dict: Callable[[dict], Any],
) -> None:
    """第三方候选源注册入口——新增 kind 的 codec 条目（fail-fast，不覆盖）。

    装配协议（与 control 层 ``register_resolver_factory`` 成对使用）：
    注册同一 kind 两次 = 装配错误，:class:`ValidationError` 当场拒绝——
    静默覆盖会让先注册的实现被悄悄顶掉，问题延迟到运行期才暴露。内置四型
    （predicate/ids/recall/fan_out）随本模块注册，第三方扩展在装配期调用
    本函数注册 codec + 调 control 层注册 resolver 工厂，两侧 kind 必须一致。
    """
    if not isinstance(kind, str) or not kind:
        raise ValidationError(f"候选源 kind 必须是非空字符串：{kind!r}")
    if kind in _CANDIDATE_CODECS:
        raise ValidationError(
            f"候选源 kind 已注册：{kind!r}（不允许覆盖；合法取值 "
            f"{'/'.join(_CANDIDATE_CODECS)}）"
        )
    _CANDIDATE_CODECS[kind] = _CandidateCodec(to_dict, from_dict)


def candidate_to_dict(source: CandidateSource) -> dict:
    """CandidateSource → dict DSL（持久化用；:func:`candidate_from_dict` 的逆）。

    filters 经 :func:`~common.type_def.filter.to_dict` 序列化为 FilterExpr
    的 dict DSL；round-trip 后语义等价（field 已规范化，canonical 幂等）。
    分派按 ``type(source).kind`` 查 ``_CANDIDATE_CODECS`` 注册表。
    """
    kind = getattr(type(source), "kind", None)
    codec = _CANDIDATE_CODECS.get(kind) if isinstance(kind, str) else None
    if codec is None:
        raise ValidationError(f"未知的候选源类型：{type(source).__name__}")
    return {"type": kind, **codec.to_dict(source)}


def _parse_shape_set(value: Any, name: str) -> frozenset[str]:
    """JSON 形状约束 → frozenset（缺省空集；非字符串列表 ValidationError）。

    先类型检查再 ``frozenset()``——字符串 ``"user"`` 会被静默拆成字符集
    ``{u,s,e,r}``，必须在转换前拦截（fail-closed）。
    """
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise ValidationError(
            f"fan_out 候选源的 {name} 必须是 scope 字符串列表：{value!r}"
        )
    return frozenset(value)


def candidate_from_dict(data: dict) -> CandidateSource:
    """dict DSL → CandidateSource（HTTP/SDK 边界解析 + 重启恢复）。

    形态（F04 D3）：``{"type": "predicate"|"ids"|"recall"|"fan_out", ...}``；
    filters 走 :func:`~common.type_def.filter.normalize`（解析 + 校验 + 规范化）。
    非法形态（未知 type / 缺必填 / filters 不合法）一律 :class:`ValidationError`。
    分派按 ``"type"`` 查 ``_CANDIDATE_CODECS`` 注册表（与 candidate_to_dict、
    control 层 resolver 工厂共用同一判别键）。
    """
    if not isinstance(data, dict):
        raise ValidationError(
            f"candidate 必须是对象（dict DSL），收到 {type(data).__name__}"
        )
    kind = data.get("type")
    codec = _CANDIDATE_CODECS.get(kind) if isinstance(kind, str) else None
    if codec is None:
        raise ValidationError(
            f"未知的候选源 type：{kind!r}（合法取值 {'/'.join(_CANDIDATE_CODECS)}）"
        )
    try:
        return codec.from_dict(data)
    except ValidationError:
        raise
    except (KeyError, TypeError) as exc:
        raise ValidationError(f"candidate 解析失败：{exc}") from None
