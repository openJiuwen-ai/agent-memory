# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""经验记忆的共用部件（mem2.0 §2.4/§3.2）。

构建侧（``construction/experience.py`` 及其算子）与检索侧
（``retrieval/selector_impl/experience_selector.py``）共用的部件都在本文件，
按节组织：

- **效用记录**：效用事件、统计记录与三种小记录的键构造、数据结构与读写门面
- **取策略取值**：排序、探索、隔离期与质量分的首版取值（随策略版本调整）
- **质量分**：有效性与新鲜度的计算
- **usage_ref 引用值**：取策略版本与探索标记的编解码（与接入方的回传约定）
- **聚合**：效用事件 → 统计记录与类别基线（周期整理第 1 步，§2.5.2/§3.2）

分层（F08）：检索层不导入构建层，本文件因此落在 common——两侧的公共下游。
``KVStore`` 只出现在类型注解中（``TYPE_CHECKING`` 导入），运行时由调用方注入
实例，common 不反向依赖 storage。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.log import get_logger

if TYPE_CHECKING:
    from jiuwen_memory.common.type_def import Scope
    from jiuwen_memory.storage.kv import KVStore

logger = get_logger(__name__)

# ====================================================================== #
# 效用记录：键构造、数据结构与读写（mem2.0 §3.2）
# ====================================================================== #
# 效用事件与三种小记录不是记忆条目：不建索引、不参与检索、``get``/``list`` 读
# 不到，读取方按键点读，全部存经验空间：
#
# | 记录 | 键 |
# |---|---|
# | 归因事件 | ``/experience/utility/{条目id}/attr:{作者摘要}:{tid}:{修订号}`` |
# | 复现事件 | ``/experience/utility/{条目id}/repro:{作者摘要}:{tid}:{修订号}`` |
# | 统计记录 | ``/experience/stats/{条目id}`` |
# | 类别基线 | ``/experience/baseline`` |
# | 复现作者记录 | ``/experience/repro-authors/{条目id}`` |
# | 类别计数记录 | ``/experience/counts/{类别}`` |
#
# v3 起经验条目可与对话记忆同空间（§2.3），专用键统一收在 ``/experience/``
# 前缀下与普通键区分，各空间分别维护。
#
# 事件键含判定修订号且只 ``insert``——同一事件重复写入（断点续做、重发）只记
# 一次。统计记录与类别基线由周期整理的聚合步骤写入（Curator，聚合纯函数
# 见本文件末节）；复现作者记录与类别计数在沉淀时增量维护。

# -- 键前缀与常量 ------------------------------------------------------ #

UTILITY_KEY_PREFIX = "/experience/utility/"
STATS_KEY_PREFIX = "/experience/stats/"
REPRO_AUTHORS_KEY_PREFIX = "/experience/repro-authors/"
CATEGORY_COUNTS_KEY_PREFIX = "/experience/counts/"
# 按作者清除的登记标记前缀（mem2.0 §2.5.2「按作者清除」）：用户空间删除后由
# 装配注入的回调写一个标记，下一轮周期整理在聚合之前消费并删除标记。
ERASED_AUTHORS_KEY_PREFIX = "/experience/erased-authors/"
BASELINE_KEY = "/experience/baseline"

# 复现作者记录至多记 2 个作者摘要：检索的复现要求是「被第二个用户独立复现才放行
# 可执行条目」（§2.4 的「风险控制」），记满 2 个即够用。
REPRO_AUTHOR_LIMIT = 2

# 归因结论（attribution 值 / 归因事件的 verdict）。
ATTR_HELPFUL = "helpful"
ATTR_HARMFUL = "harmful"
ATTR_NEUTRAL = "neutral"

# 轨迹成败判定的结论（VerdictRecord.outcome / 复现事件的 outcome）。定义在
# common：判定侧（construction）与效用聚合两侧共用同一取值域；部分成功在
# 下游一律按未成功处理（§3.1）。
OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILURE = "failure"

# 证据等级（VerdictRecord.evidence / 归因事件的 evidence），等级越高越可靠。
EVIDENCE_EXTERNAL = "external"
EVIDENCE_USER_FEEDBACK = "user_feedback"
EVIDENCE_TOOL_ERROR = "tool_error"
EVIDENCE_MODEL_ONLY = "model_only"


def now_iso() -> str:
    """当前时刻的 ISO 8601 字符串（UTC，记录各时间字段统一用它）。"""
    return datetime.now(UTC).isoformat()


def encode_payload(payload: Any) -> bytes:
    """记录值统一编码：紧凑 JSON（UTF-8）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_payload(raw: bytes) -> Any:
    """记录值统一解码（``encode_payload`` 的逆）。"""
    return json.loads(raw.decode("utf-8"))


# -- 键构造 ------------------------------------------------------------- #


def attribution_event_key(
    entry_id: str, author_digest: str, trajectory_id: str, revision: int
) -> str:
    """归因事件的键：``/experience/utility/{条目id}/attr:{作者摘要}:{tid}:{修订号}``。"""
    return f"{UTILITY_KEY_PREFIX}{entry_id}/attr:{author_digest}:{trajectory_id}:{revision}"


def reproduction_event_key(
    entry_id: str, author_digest: str, trajectory_id: str, revision: int
) -> str:
    """复现事件的键：``/experience/utility/{条目id}/repro:{作者摘要}:{tid}:{修订号}``。"""
    return f"{UTILITY_KEY_PREFIX}{entry_id}/repro:{author_digest}:{trajectory_id}:{revision}"


def stats_key(entry_id: str) -> str:
    """统计记录的键：``/experience/stats/{条目id}``。首版由周期整理的聚合步骤写入（§3.2）。"""
    return f"{STATS_KEY_PREFIX}{entry_id}"


def repro_authors_key(entry_id: str) -> str:
    """复现作者记录的键：``/experience/repro-authors/{条目id}``，值是至多 2 个作者摘要。"""
    return f"{REPRO_AUTHORS_KEY_PREFIX}{entry_id}"


def erased_author_key(digest: str) -> str:
    """按作者清除标记的键：``/experience/erased-authors/{作者摘要}``（§2.5.2「按作者清除」）。"""
    return f"{ERASED_AUTHORS_KEY_PREFIX}{digest}"


def category_count_key(category: str) -> str:
    """类别计数记录的键：``/experience/counts/{类别}``。"""
    return f"{CATEGORY_COUNTS_KEY_PREFIX}{category}"


def baseline_key() -> str:
    """类别基线的键：``/experience/baseline``，值是各类别有效性的中位数。"""
    return BASELINE_KEY


# -- 记录数据类 ---------------------------------------------------------- #


@dataclass
class AttributionEvent:
    """归因事件（§3.2）：一条轨迹取用了某条条目之后的结论。"""

    verdict: str = ATTR_NEUTRAL     # helpful / harmful / neutral
    evidence: str = EVIDENCE_MODEL_ONLY  # 来源轨迹的证据等级
    author: str = ""                # 作者摘要（不是作者主体）
    retrieval_version: str = "0"    # 使用记录 ref 解出的取策略版本号
    explore: bool = False           # ref 标记是否为探索名额
    occurred_at: str = ""


@dataclass
class ReproductionEvent:
    """复现事件（§3.2）：一条轨迹又做出了某条条目所讲的做法。"""

    source_scope: list[str] = field(default_factory=list)  # 来源轨迹写入落点五元组
    trajectory_id: str = ""
    author: str = ""                # 作者摘要
    retrieved_ids: list[str] = field(default_factory=list)  # 该轨迹的使用记录
    outcome: str = OUTCOME_PARTIAL   # 来源轨迹的判定结论
    evidence: str = EVIDENCE_MODEL_ONLY
    occurred_at: str = ""


@dataclass
class StatsRecord:
    """统计记录（§3.2）：每条条目一份的效用汇总，由周期整理的聚合步骤写入。

    首版检索只消费 ``covered`` / ``helpful`` / ``harmful`` / ``authors`` /
    ``last_positive_at`` 五个字段（复现要求、质量分与探索阈值）；其余字段为
    周期整理与升为技能的条件检查预留，结构在此一并定型。
    """

    helpful: dict[str, int] = field(default_factory=dict)   # 按证据等级分的计数
    harmful: dict[str, int] = field(default_factory=dict)   # 同上
    covered: int = 0             # 归因覆盖次数（只计有帮助与有害）
    neutral: int = 0             # 判为不计的次数
    authors: list[str] = field(default_factory=list)        # 复现过的作者摘要
    author_success: dict[str, int] = field(default_factory=dict)  # 各作者的成功复现次数
    strong_success: int = 0      # external / user_feedback 级的成功次数
    last_used_at: str = ""       # 最近一次出现在使用记录中
    last_positive_at: str = ""   # 最近一次被复现或被判有帮助
    merged_into: str = ""        # 合并去向
    competition_group: str = ""  # 所属竞争组（§2.5.2）
    sealed: dict[str, int] = field(default_factory=dict)    # 已删除旧事件的累计
    watermark: str = ""          # 本次聚合覆盖到的时间


# -- 读写门面 ------------------------------------------------------------ #


class UtilityStore:
    """效用事件与三种小记录的读写：全部在经验空间 scope 下。

    事件键含判定修订号且只 ``insert``——同一事件重复写入（断点续做、重发）只记
    一次，键已存在即已记过。复现作者记录与类别计数是读改写，并发沉淀时可能少计，
    每轮周期整理校正（§2.5.2）。统计记录与类别基线由周期整理的聚合步骤写入
    （:meth:`save_stats` / :meth:`save_baseline`，§3.2），读取方是检索的质量分
    与复现要求（§2.4）。
    """

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    # -- 效用事件（只追加） --------------------------------------------- #

    def write_attribution_event(
        self,
        scope: Scope,
        entry_id: str,
        author_digest: str,
        trajectory_id: str,
        revision: int,
        event: AttributionEvent,
    ) -> bool:
        """写一条归因事件；同一事件已记过时返回 ``False``（幂等，不重复计）。"""
        key = attribution_event_key(entry_id, author_digest, trajectory_id, revision)
        return self._append_event(scope, key, asdict(event))

    def write_reproduction_event(
        self,
        scope: Scope,
        entry_id: str,
        author_digest: str,
        trajectory_id: str,
        revision: int,
        event: ReproductionEvent,
    ) -> bool:
        """写一条复现事件；同一事件已记过时返回 ``False``（幂等）。"""
        key = reproduction_event_key(entry_id, author_digest, trajectory_id, revision)
        return self._append_event(scope, key, asdict(event))

    def _append_event(self, scope: Scope, key: str, payload: dict) -> bool:
        try:
            self._kv.insert(scope, key, encode_payload(payload))
        except ConflictError:
            logger.debug("UtilityStore: event already recorded, key=%s", key[:64])
            return False
        return True

    # -- 复现作者记录 ---------------------------------------------------- #

    def load_repro_authors(self, scope: Scope, entry_id: str) -> list[str]:
        """读条目的复现作者摘要列表；无记录返回空列表。"""
        try:
            raw = self._kv.get(scope, repro_authors_key(entry_id))
        except NotFoundError:
            return []
        data = decode_payload(raw)
        if not isinstance(data, list):
            return []
        return [str(item) for item in data]

    def save_repro_authors(self, scope: Scope, entry_id: str, digests: list[str]) -> None:
        """写复现作者摘要列表（至多 :data:`REPRO_AUTHOR_LIMIT` 个，超出的丢弃）。"""
        kept = digests[:REPRO_AUTHOR_LIMIT]
        key = repro_authors_key(entry_id)
        try:
            self._kv.update(scope, key, encode_payload(kept))
        except NotFoundError:
            self._kv.insert(scope, key, encode_payload(kept))

    def add_repro_author(self, scope: Scope, entry_id: str, digest: str) -> list[str]:
        """把一个作者摘要并入条目的复现作者记录，返回并入后的列表。

        已在列表中时不重复添加；至多保留 :data:`REPRO_AUTHOR_LIMIT` 个。返回值
        与落盘内容一致（截断后的列表），调用方按它判断「是否已有两个独立作者」。
        """
        authors = self.load_repro_authors(scope, entry_id)
        if digest not in authors:
            authors.append(digest)
        kept = authors[:REPRO_AUTHOR_LIMIT]
        self.save_repro_authors(scope, entry_id, kept)
        return kept

    # -- 按作者清除（mem2.0 §2.5.2「按作者清除」） ------------------------ #

    def add_erased_author(self, scope: Scope, digest: str) -> None:
        """登记一个待清除作者（空间删除回调写入；幂等——同摘要覆盖）。"""
        key = erased_author_key(digest)
        payload = encode_payload({"registered_at": now_iso()})
        try:
            self._kv.update(scope, key, payload)
        except NotFoundError:
            self._kv.insert(scope, key, payload)

    def load_erased_authors(self, scope: Scope) -> list[str]:
        """读全部待清除作者的摘要（周期整理聚合之前消费）。"""
        digests: list[str] = []
        prefix = ERASED_AUTHORS_KEY_PREFIX
        for key, _raw in self._kv.scan(scope, prefix):
            digest = key[len(prefix):]
            if digest and digest not in digests:
                digests.append(digest)
        return digests

    def drop_erased_author(self, scope: Scope, digest: str) -> None:
        """删除一个清除标记（该作者的清除已处理完）。"""
        try:
            self._kv.delete(scope, erased_author_key(digest))
        except NotFoundError:
            pass

    def delete_events_by_authors(self, scope: Scope, digests) -> int:
        """删除给定作者的效用事件（归因与复现），返回删除条数。

        标记键的布局损坏（解析不出作者）不删——删除是合规操作，宁可漏删
        留待下轮，不可按错误对象多删。
        """
        wanted = set(digests)
        removed = 0
        for key, _raw in self._kv.scan(scope, UTILITY_KEY_PREFIX):
            parsed = parse_utility_event_key(key)
            if parsed is None or parsed.author not in wanted:
                continue
            self._kv.delete(scope, key)
            removed += 1
        return removed

    # -- 类别计数记录 ---------------------------------------------------- #

    def load_category_count(self, scope: Scope, category: str) -> dict[str, int]:
        """读类别计数 ``{"active": 生效条数, "dropped": 因写入上限丢弃的条数}``。"""
        try:
            raw = self._kv.get(scope, category_count_key(category))
        except NotFoundError:
            return {"active": 0, "dropped": 0}
        data = decode_payload(raw)
        if not isinstance(data, dict):
            return {"active": 0, "dropped": 0}
        return {
            "active": int(data.get("active", 0)),
            "dropped": int(data.get("dropped", 0)),
        }

    def add_category_count(
        self, scope: Scope, category: str, *, active: int = 0, dropped: int = 0
    ) -> dict[str, int]:
        """累加类别计数（沉淀收尾每新增一条 active 加一；丢弃时 dropped 加一）。

        周期整理的第 8 步按实际条数整体覆写（Curator），此处只做增量。
        """
        counts = self.load_category_count(scope, category)
        counts["active"] += active
        counts["dropped"] += dropped
        key = category_count_key(category)
        try:
            self._kv.update(scope, key, encode_payload(counts))
        except NotFoundError:
            self._kv.insert(scope, key, encode_payload(counts))
        return counts

    def save_category_count(self, scope: Scope, category: str, counts: Mapping[str, int]) -> None:
        """整体覆写类别计数（周期整理第 8 步）：active 按实际条数，dropped 沿用累计值。"""
        key = category_count_key(category)
        payload = encode_payload(
            {
                "active": int(counts.get("active", 0)),
                "dropped": int(counts.get("dropped", 0)),
            }
        )
        try:
            self._kv.update(scope, key, payload)
        except NotFoundError:
            self._kv.insert(scope, key, payload)

    # -- 统计记录与类别基线（周期整理写，检索读） ------------------------- #

    def load_stats(self, scope: Scope, entry_id: str) -> StatsRecord | None:
        """读条目的统计记录；无记录返回 ``None``（条目取类别基线，§2.4）。

        记录损坏（非法 JSON / 非对象）不抛：一条坏记录炸掉整个经验检索不可取，
        记 WARNING 后按无统计记录处理——质量分回落基线，复现要求回落复现作者
        记录，两个方向都偏保守。
        """
        try:
            raw = self._kv.get(scope, stats_key(entry_id))
        except NotFoundError:
            return None
        try:
            data = decode_payload(raw)
        except ValueError:
            logger.warning("UtilityStore: stats record corrupted, entry=%s", entry_id)
            return None
        if not isinstance(data, dict):
            logger.warning("UtilityStore: stats record not an object, entry=%s", entry_id)
            return None
        return StatsRecord(
            helpful={
                str(k): int(v) for k, v in dict(data.get("helpful", {})).items()
            },
            harmful={
                str(k): int(v) for k, v in dict(data.get("harmful", {})).items()
            },
            covered=int(data.get("covered", 0)),
            neutral=int(data.get("neutral", 0)),
            authors=[str(a) for a in list(data.get("authors", []))],
            author_success={
                str(k): int(v) for k, v in dict(data.get("author_success", {})).items()
            },
            strong_success=int(data.get("strong_success", 0)),
            last_used_at=str(data.get("last_used_at", "")),
            last_positive_at=str(data.get("last_positive_at", "")),
            merged_into=str(data.get("merged_into", "")),
            competition_group=str(data.get("competition_group", "")),
            sealed={str(k): int(v) for k, v in dict(data.get("sealed", {})).items()},
            watermark=str(data.get("watermark", "")),
        )

    def load_baseline(self, scope: Scope) -> dict[str, float]:
        """读类别基线（``/experience/baseline``）：各类别条目有效性的中位数。

        无记录返回空字典——检索侧逐类别回退到 :data:`DEFAULT_BASELINE`（冷启动
        中性值）。记录损坏同样按无记录处理，理由同 :meth:`load_stats`。
        """
        try:
            raw = self._kv.get(scope, baseline_key())
        except NotFoundError:
            return {}
        try:
            data = decode_payload(raw)
        except ValueError:
            logger.warning("UtilityStore: baseline record corrupted")
            return {}
        if not isinstance(data, dict):
            logger.warning("UtilityStore: baseline record not an object")
            return {}
        return {
            str(key): float(value)
            for key, value in data.items()
            if isinstance(value, (int, float))
        }

    def save_stats(self, scope: Scope, entry_id: str, stats: StatsRecord) -> None:
        """写条目的统计记录（聚合步骤的产出，每轮整体覆写，§3.2）。"""
        key = stats_key(entry_id)
        payload = encode_payload(asdict(stats))
        try:
            self._kv.update(scope, key, payload)
        except NotFoundError:
            self._kv.insert(scope, key, payload)

    def save_baseline(self, scope: Scope, baseline: Mapping[str, float]) -> None:
        """写类别基线（``/experience/baseline``），整体覆写（聚合步骤的产出，§3.2）。"""
        payload = encode_payload({str(k): float(v) for k, v in baseline.items()})
        try:
            self._kv.update(scope, BASELINE_KEY, payload)
        except NotFoundError:
            self._kv.insert(scope, BASELINE_KEY, payload)

    def scan_events(self, scope: Scope) -> list[tuple[UtilityEventKey, dict[str, Any]]]:
        """按前缀扫出全部效用事件并解出键与载荷（聚合的输入，§2.5.2 第 1 步）。

        键不符合布局或载荷损坏的事件跳过并记 WARNING：个别坏记录不中断整轮
        聚合，跳过的事件在下轮聚合仍会被扫到。
        """
        scanned: list[tuple[UtilityEventKey, dict[str, Any]]] = []
        for key, raw in self._kv.scan(scope, UTILITY_KEY_PREFIX):
            parsed = parse_utility_event_key(key)
            if parsed is None:
                logger.warning(
                    "UtilityStore: malformed utility event key, skipped: %s", key[:96]
                )
                continue
            try:
                payload = decode_payload(raw)
            except ValueError:
                logger.warning(
                    "UtilityStore: utility event payload corrupted, skipped: %s", key[:96]
                )
                continue
            if not isinstance(payload, dict):
                logger.warning(
                    "UtilityStore: utility event payload not an object, skipped: %s", key[:96]
                )
                continue
            scanned.append((parsed, payload))
        return scanned


# ====================================================================== #
# 取策略的取值（mem2.0 §2.4，随策略版本调整）
# ====================================================================== #
# 首版（策略版本 0）的内置默认值。检索侧的选取算子按它们排序与取舍；证据等级
# 的权重属判策略，聚合侧（周期整理）加权计数时也用同一张表。

# 证据等级的权重：一次有帮助或有害按多少次计（判策略）。
EVIDENCE_WEIGHTS: dict[str, float] = {
    EVIDENCE_EXTERNAL: 1.0,
    EVIDENCE_USER_FEEDBACK: 1.0,
    EVIDENCE_TOOL_ERROR: 0.7,
    EVIDENCE_MODEL_ONLY: 0.4,
}
# 有效性公式的平滑强度 k：先当作每条条目已有 k 次与同类平均水平相同的结果，
# 使次数少的条目不被一两次结果带偏。
SMOOTHING_K = 2.0
# 质量分的两个权重：有效性与新鲜度的加权和。
QUALITY_VALIDITY_WEIGHT = 0.7
QUALITY_FRESHNESS_WEIGHT = 0.3
# 新鲜度的半衰期（天）：按最近一次被复现或被判有帮助的时间指数衰减。
FRESHNESS_HALF_LIFE_DAYS = 30.0
# 排序分的两个指数：相关性取平方，使匹配程度的差别比质量的差别更起作用。
RANK_ALPHA = 2.0
RANK_BETA = 1.0
# 探索阈值与探索名额：归因次数（统计记录的 covered）低于阈值的条目算未经验证，
# 每次返回留出的条数。
EXPLORATION_THRESHOLD = 5
EXPLORATION_QUOTA = 1
# 隔离期（天）：新条目落盘后的一段时间只经探索名额返回，每次检索至多一条。
ISOLATION_PERIOD_DAYS = 7
# 类别基线的冷启动默认值：周期聚合尚未产出基线时，无统计记录的条目取它
# （设计文档未定首版取值，中性 0.5，见 F08 决策）。
DEFAULT_BASELINE = 0.5


# ====================================================================== #
# 质量分（mem2.0 §2.4）
# ====================================================================== #


def weighted_count(counts: Mapping[str, int]) -> float:
    """按证据等级的权重把一份计数表折成加权次数。"""
    return sum(EVIDENCE_WEIGHTS.get(level, 0.0) * int(n) for level, n in counts.items())


def validity(stats: StatsRecord, baseline: float) -> float:
    """有效性（§2.4）：条目被用过之后判为有帮助的占比，0 到 1。

    ``(加权有帮助 + k × 基线) ÷ (加权有帮助 + 加权有害 + k)``。基线取同类别
    条目有效性的中位数；无统计记录的条目直接取基线（公式在零计数下退化为
    基线本身）。分母恒含 k，不会除零。
    """
    positive = weighted_count(stats.helpful)
    negative = weighted_count(stats.harmful)
    return (positive + SMOOTHING_K * baseline) / (positive + negative + SMOOTHING_K)


def days_since(iso_at: str, *, now: datetime) -> float | None:
    """从 ISO 8601 时刻到 ``now`` 的天数（负值取 0）；解析失败返回 ``None``。

    新鲜度与隔离期共用（隔离期按落盘时间起算，§2.4）。
    """
    try:
        at = datetime.fromisoformat(iso_at)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (now - at).total_seconds() / 86400.0)


def freshness(last_positive_at: str, fallback_at: str, *, now: datetime) -> float:
    """新鲜度（§2.4）：``2^(−距今天数 ÷ 半衰期)``。

    距今按最近一次被复现或被判有帮助的时间起算；没有时取落盘时间。两个时间
    都缺失或不可解析时取 1.0（按最新鲜处理——新条目本就该有高新鲜度，且隔离
    期机制另有限制，不会因此放行旧条目）。
    """
    for iso_at in (last_positive_at, fallback_at):
        if not iso_at:
            continue
        days = days_since(iso_at, now=now)
        if days is not None:
            return 2.0 ** (-days / FRESHNESS_HALF_LIFE_DAYS)
    return 1.0


def quality_score(entry_validity: float, entry_freshness: float) -> float:
    """质量分（§2.4）：有效性与新鲜度的加权和，与本次检索句无关。"""
    return QUALITY_VALIDITY_WEIGHT * entry_validity + QUALITY_FRESHNESS_WEIGHT * entry_freshness


# ====================================================================== #
# usage_ref 引用值（mem2.0 §2.4，与接入方的回传约定）
# ====================================================================== #
# 检索返回时编码进条目的 system_metadata：格式为「取策略版本号.标记」，标记
# ``e`` 表示经探索名额选出、``n`` 表示按排序分选出。agent 随使用记录原样带回，
# 沉淀时解出记在归因事件上（§3.2）。取策略版本首版恒为 0；就生成并要求带回，
# 是因为它属于与接入方的约定。

# 取策略版本：首版恒为 0（策略层预留）。
USAGE_REF_VERSION = "0"
USAGE_REF_EXPLORE = "e"
USAGE_REF_NORMAL = "n"


def encode_usage_ref(*, explore: bool, version: str = USAGE_REF_VERSION) -> str:
    """编码引用值：``0.e``（探索名额）/ ``0.n``（按排序分）。"""
    mark = USAGE_REF_EXPLORE if explore else USAGE_REF_NORMAL
    return f"{version}.{mark}"


def parse_usage_ref(ref: str) -> tuple[str, bool]:
    """解出 ``(取策略版本号, 是否探索名额)``；解析失败按版本 0、非探索处理。

    首版只解出记在归因事件上，策略层不消费（§3.2）。
    """
    parts = str(ref).strip().split(".")
    if len(parts) == 2 and parts[0] and parts[1] in {USAGE_REF_NORMAL, USAGE_REF_EXPLORE}:
        return parts[0], parts[1] == USAGE_REF_EXPLORE
    return USAGE_REF_VERSION, False


# ====================================================================== #
# 聚合：效用事件 → 统计记录与类别基线（mem2.0 §2.5.2 第 1 步 / §3.2）
# ====================================================================== #
# 周期整理（Curator）每轮把经验空间里的全部效用事件聚合为每条条目一份
# 的统计记录，再由统计与条目类别算出各类别基线。两个函数都是纯函数：输入
# 扫描出的事件与重定向映射，输出内存中的记录，落盘由调用方经 ``UtilityStore``
# 完成——聚合本身可单测、可重放。

# 归因事件的作者限计窗口（天）：同一作者对同一条目的有帮助与有害在此窗口内各
# 至多计一次（§3.2）。``repro_unit=trajectory`` 的部署按轨迹计数、不按作者
# 限计，聚合时传 ``window_days=None``。
ATTRIBUTION_WINDOW_DAYS = 7.0


@dataclass(frozen=True)
class UtilityEventKey:
    """效用事件键的解析结果（:func:`attribution_event_key` 等键构造的逆）。"""

    entry_id: str
    kind: str          # "attr"（归因）/ "repro"（复现）
    author: str        # 作者摘要
    trajectory_id: str
    revision: int      # 判定修订号


_UTILITY_EVENT_KEY_RE = re.compile(
    r"^/experience/utility/(?P<entry>[^/]+)/(?P<kind>attr|repro):(?P<author>[^:]*):(?P<tid>[^:]*):(?P<rev>\d+)$"
)


def parse_utility_event_key(key: str) -> UtilityEventKey | None:
    """解析效用事件键；不符合键布局返回 ``None``（调用方跳过，不中断聚合）。"""
    match = _UTILITY_EVENT_KEY_RE.match(key)
    if match is None:
        return None
    return UtilityEventKey(
        entry_id=match.group("entry"),
        kind=match.group("kind"),
        author=match.group("author"),
        trajectory_id=match.group("tid"),
        revision=int(match.group("rev")),
    )


def _resolve_redirect(entry_id: str, redirects: Mapping[str, str]) -> str:
    """解出事件的计入条目：被合并 / 被取代条目的事件计入去向（§3.2）。

    链式重定向一路跟到头（A 并入 B、B 又并入 C 时 A 计入 C）；映射成环时止于
    当前值，不死循环（环本身是脏数据，防御性处理）。
    """
    seen: set[str] = set()
    current = entry_id
    while current in redirects and current not in seen:
        seen.add(current)
        current = redirects[current]
    return current


def _iso_gap_days(later: str, earlier: str) -> float:
    """两个 ISO 8601 时刻相差的天数；任一不可解析返回 ``-1.0``（按窗口内处理）。"""
    try:
        after = datetime.fromisoformat(later)
        before = datetime.fromisoformat(earlier)
    except ValueError:
        return -1.0
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    if before.tzinfo is None:
        before = before.replace(tzinfo=UTC)
    return (after - before).total_seconds() / 86400.0


def aggregate_utility(
    events: Sequence[tuple[UtilityEventKey, Mapping[str, Any]]],
    *,
    redirects: Mapping[str, str] | None = None,
    window_days: float | None = ATTRIBUTION_WINDOW_DAYS,
    prior: Mapping[str, StatsRecord] | None = None,
    watermark: str = "",
) -> dict[str, StatsRecord]:
    """把效用事件聚合为每条条目一份的统计记录（§3.2，周期整理第 1 步）。

    规则：

    - 同 (条目, 类型, 作者, 轨迹) 只取修订号最大的一条——同一事件以最新一轮
      判定为准；
    - 事件条目经 ``redirects`` 重定向后计入去向条目（被合并计入 ``merged_into``
      去向、被取代计入新条目，映射由周期整理的合并步骤构造）；
    - ``window_days`` 非 ``None`` 时，同一作者对同一条目的有帮助与有害在窗口
      内各至多计一次；``repro_unit=trajectory`` 的部署传 ``None`` 不按作者
      限计（复现事件与 neutral 结论始终不限计）；
    - ``merged_into`` / ``competition_group`` / ``sealed`` 不是聚合的产物（来自
      合并步骤与按作者清除），从 ``prior`` 原样带过；
    - ``watermark`` 原样写入每份记录（本轮聚合覆盖到的时间）。

    只做统计、不碰存储：事件由 :meth:`UtilityStore.scan_events` 扫出，落盘由
    调用方完成。返回的字典按计入条目 id 索引。``redirects`` / ``prior`` 缺省
    ``None`` 视为空映射（G.FNM.01 不用可变字面量作默认值）。
    """
    redirects = redirects or {}
    prior = prior or {}
    # 同 (条目, 类型, 作者, 轨迹) 取最新修订。
    latest: dict[tuple[str, str, str, str], tuple[UtilityEventKey, Mapping[str, Any]]] = {}
    for key, payload in events:
        group = (key.entry_id, key.kind, key.author, key.trajectory_id)
        held = latest.get(group)
        if held is None or key.revision > held[0].revision:
            latest[group] = (key, payload)

    # 归因事件的作者限计：重定向后按 (条目, 作者, 结论) 分桶，桶内按发生时间
    # 排序做冷却去重（计一次后窗口内的跳过）。复现事件与 neutral 结论不限计。
    counted: list[tuple[str, UtilityEventKey, Mapping[str, Any]]] = []
    windowed: dict[
        tuple[str, str, str], list[tuple[str, UtilityEventKey, Mapping[str, Any]]]
    ] = {}
    for key, payload in latest.values():
        entry_id = _resolve_redirect(key.entry_id, redirects)
        verdict = str(payload.get("verdict", ATTR_NEUTRAL))
        if (
            key.kind == "attr"
            and window_days is not None
            and verdict in (ATTR_HELPFUL, ATTR_HARMFUL)
        ):
            occurred_at = str(payload.get("occurred_at", ""))
            windowed.setdefault((entry_id, key.author, verdict), []).append(
                (occurred_at, key, payload)
            )
        else:
            counted.append((entry_id, key, payload))
    if window_days is not None:
        for (entry_id, _author, _verdict), bucket in windowed.items():
            bucket.sort(key=lambda item: item[0])  # ISO 8601 字典序即时间序
            last_counted_at: str | None = None
            for occurred_at, key, payload in bucket:
                if last_counted_at is None or (
                    _iso_gap_days(occurred_at, last_counted_at) >= window_days
                ):
                    counted.append((entry_id, key, payload))
                    last_counted_at = occurred_at

    # 累计：归因计 helpful / harmful / neutral，复现计 authors / author_success /
    # strong_success；prior 只带过三个非聚合产物字段。
    records: dict[str, StatsRecord] = {}

    def _record_for(entry_id: str) -> StatsRecord:
        record = records.get(entry_id)
        if record is None:
            previous = prior.get(entry_id)
            record = StatsRecord(
                merged_into=previous.merged_into if previous is not None else "",
                competition_group=(
                    previous.competition_group if previous is not None else ""
                ),
                sealed=dict(previous.sealed) if previous is not None else {},
            )
            records[entry_id] = record
        return record

    for entry_id, key, payload in counted:
        record = _record_for(entry_id)
        occurred_at = str(payload.get("occurred_at", ""))
        if key.kind == "attr":
            verdict = str(payload.get("verdict", ATTR_NEUTRAL))
            level = str(payload.get("evidence", EVIDENCE_MODEL_ONLY))
            if verdict == ATTR_HELPFUL:
                record.helpful[level] = record.helpful.get(level, 0) + 1
                record.covered += 1
                record.last_positive_at = max(record.last_positive_at, occurred_at)
            elif verdict == ATTR_HARMFUL:
                record.harmful[level] = record.harmful.get(level, 0) + 1
                record.covered += 1
            else:
                record.neutral += 1
        else:
            if key.author and key.author not in record.authors:
                record.authors.append(key.author)
            if str(payload.get("outcome", "")) == OUTCOME_SUCCESS:
                if key.author:
                    record.author_success[key.author] = (
                        record.author_success.get(key.author, 0) + 1
                    )
                if str(payload.get("evidence", "")) in (
                    EVIDENCE_EXTERNAL,
                    EVIDENCE_USER_FEEDBACK,
                ):
                    record.strong_success += 1
                record.last_positive_at = max(record.last_positive_at, occurred_at)
        record.last_used_at = max(record.last_used_at, occurred_at)

    for record in records.values():
        record.watermark = watermark
    return records


def compute_baseline(
    stats: Mapping[str, StatsRecord],
    entry_categories: Mapping[str, str],
    *,
    prior_baseline: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """类别基线：各类别条目有效性的中位数（§2.4/§3.2）。

    有效性公式以基线为平滑项，本轮基线用上一轮的（无则取
    :data:`DEFAULT_BASELINE`）计算——迭代收敛，写入后供下一轮检索与聚合使用。
    ``entry_categories`` 里没有类别的条目不参与；没有条目的类别不出现在结果里。
    ``prior_baseline`` 缺省 ``None`` 视为空映射。
    """
    prior_baseline = prior_baseline or {}
    values_by_category: dict[str, list[float]] = {}
    for entry_id, record in stats.items():
        category = str(entry_categories.get(entry_id, "")).strip()
        if not category:
            continue
        base = prior_baseline.get(category, DEFAULT_BASELINE)
        values_by_category.setdefault(category, []).append(validity(record, base))
    return {
        category: median(values) for category, values in values_by_category.items()
    }
