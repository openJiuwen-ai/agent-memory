# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""经验记忆的构建侧部件（mem2.0 §2.1/§2.2/§3.1）。

评价算子（``appraiser_impl/experience_appraiser.py``）与抽取算子
（``extractor_impl/experience_extractor.py``）共用的部件都在本文件，按节组织：

- **轨迹与判定记录**：两种记录的读写（效用记录与共用部件见
  ``common/experience.py``——检索侧也读它们，按 F09 不落在本层）
- **报错规则**：``tool_error`` 级证据、值得提炼的「有报错」
- **轨迹消息**：最后一段结果、本次新输入、执行过程压缩
- **密钥清洗与脱敏检查**：轨迹落盘前清洗、经验条目落盘前丢弃判定
- **作者摘要**：作者主体经部署密钥做 HMAC、作者主体推导
- **按作者清除的登记回调**：用户空间删除后在经验空间写清除标记（§2.5.2）
- **评价结论**：一次轨迹评价的全部结论的数据类
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.experience import (
    EVIDENCE_EXTERNAL,
    EVIDENCE_MODEL_ONLY,
    EVIDENCE_TOOL_ERROR,
    EVIDENCE_USER_FEEDBACK,
    OUTCOME_PARTIAL,
    UtilityStore,
    decode_payload,
    encode_payload,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.raw import trajectory_key
from jiuwen_memory.storage.kv import KVStore

if TYPE_CHECKING:
    from jiuwen_memory.construction.router import SpaceNaming

logger = get_logger(__name__)

# ====================================================================== #
# 记录：轨迹与判定记录的键构造及读写（mem2.0 §2.1/§3.1）
# ====================================================================== #
# 这些记录不是记忆条目：不建索引、不参与检索、``get``/``list`` 读不到，读取方都
# 按键点读。轨迹记录与判定记录存**写入落点**——与对话原文同一个 KV、同一个
# scope，只是键前缀不同，空间删除时随空间清除；保留期 30 天。
#
# 效用域的记录（效用事件、统计记录、类别基线、复现作者与类别计数）定义在
# ``common/experience.py``：检索侧的选取算子也读它们（§2.4），按 F09 的分层
# 约束不落在本层。

# -- 键前缀与常量 ------------------------------------------------------ #

VERDICTS_KEY_PREFIX = "/verdicts/"

# 轨迹保留期：30 天 = 周期整理间隔（缺省 7 天）的 4 倍加 2 天，使较早用户的
# 来源轨迹在技能归纳时仍可读（§2.5.3）。
TRAJECTORY_TTL_SECONDS = 30 * 24 * 3600
# 某类别的写入上限：计数达到该值时抽取算子丢弃该类别的候选，不进入去重判定
# （§2.5.2）。两轮整理之间由它限制单轮读入的条目数有上界。
CATEGORY_WRITE_LIMIT = 850

# 轨迹处理状态（ExecutionTrajectoryRecord.status）。
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
# 处理失败的可重试次数上限（§2.2「耗时与重试」：重发的请求至多 3 次）。
MAX_RETRIES = 3

# 判定结论 OUTCOME_* 定义在 ``common/experience.py``：效用聚合判断复现成败
# 也用同一取值域（§3.2）。
# 证据等级的可靠度排序（常量定义在 ``common/experience.py``，构建与检索共用），
# 判定更新时高等级取代低等级。
EVIDENCE_RANK: dict[str, int] = {
    EVIDENCE_MODEL_ONLY: 0,
    EVIDENCE_TOOL_ERROR: 1,
    EVIDENCE_USER_FEEDBACK: 2,
    EVIDENCE_EXTERNAL: 3,
}
# 判定方式（VerdictRecord.judged_by）：规则、模型或外部补报。
JUDGED_BY_MODEL = "model"
JUDGED_BY_RULE = "rule"
JUDGED_BY_EXTERNAL = "external"


def _scope_from_raw(raw: Any) -> Scope:
    """从序列化负载重建 ``Scope``；形态不符返回空 Scope（缺字段按空串）。"""
    if not isinstance(raw, Mapping):
        return Scope()
    return Scope(
        org=str(raw.get("org", "")),
        user=str(raw.get("user", "")),
        agent=str(raw.get("agent", "")),
        session=str(raw.get("session", "")),
    )


def _ttl_until(expires_at: str) -> float:
    """从 ``expires_at``（ISO 8601）计算剩余保留秒数；已过期按 0（写后即过期）。"""
    try:
        end = datetime.fromisoformat(expires_at)
    except ValueError:
        return 0.0
    now = datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0.0, (end - now).total_seconds())


# -- 键构造 ------------------------------------------------------------- #


def verdict_key(digest: str, trajectory_id: str) -> str:
    """判定记录的键：写入落点下 ``/verdicts/{作者摘要}/{trajectory_id}``。"""
    return f"{VERDICTS_KEY_PREFIX}{digest}/{trajectory_id}"


# -- 记录数据类 ---------------------------------------------------------- #


@dataclass
class ExecutionTrajectoryRecord:
    """一条轨迹的落盘记录（§2.1）。只保存落盘后还有读取方的字段。

    ``previous_trajectory_id``、``retrieved_count``、``coords`` 只在本次处理时用，
    不保存。读取方：重复检查、判定更新后的补做（复现补写与重新归因，见
    ``ExperienceAppraiser._post_verdict_update``）、技能归纳、离线评估。

    一条轨迹涉及两个主体（§2.1「生产者与归属主体」），都来自经过认证或鉴权的
    输入，不取请求自报的字符串：归属主体 ``user`` 取请求 ``scope`` 的
    org/user/agent/session 四维（不含 space——space 是落点不是归属）；生产者
    ``producer`` 取认证结果 ``AuthContext.actor``，由接口层写进内核保留键、
    评价算子从键与 ``scope.org`` 组出。作者不落盘：由 ``user`` 推导
    （:func:`author_principal`），判定记录的键与效用事件的键在存取两侧现算。
    """

    # ---- 标识与主体 ----
    trajectory_id: str = ""  # 任务标识，与 agent-core Trajectory.trajectory_id 一致
    user: Scope = field(default_factory=Scope)   # 归属主体（请求 scope 四维）
    producer: Scope = field(default_factory=Scope)  # 生产者（认证 actor：org/user/agent）
    credential_id: str = ""   # 生产者所用凭据的指纹；凭据撤销后清查经它写入的轨迹

    # ---- 请求带来的字段 ----
    messages: list[dict] = field(default_factory=list)          # 消息数组（已清洗密钥）
    capabilities: list[dict] = field(default_factory=list)      # 元素为 {kind, name, description}
    retrieved: list[dict] | None = None                         # 元素为 {id, ref}；None=不提供
    tool_call_count: int = 0                                    # 工具调用次数
    external_score: float | None = None                         # 外部验证结果；None=无
    context: dict = field(default_factory=dict)  # 背景信息（§2.1）；未提供为空

    # ---- 处理状态，由 mem2.0 维护 ----
    status: str = STATUS_PROCESSING  # processing / done / failed
    retries: int = 0                 # 已重试次数
    started_at: str = ""             # 本次处理的开始时间，ISO 8601
    expires_at: str = ""             # 过期时间，ISO 8601

    @property
    def producer_principal(self) -> str:
        """生产者主体（``user:<id>`` / ``agent:<id>``）：补报核对与外部阈值都按它取值。"""
        return author_principal(user=self.producer.user, agent=self.producer.agent)


@dataclass
class VerdictRecord:
    """一条轨迹的成败判定（§3.1）。

    读取方：提炼（抽取算子）、归因、升为技能的条件检查；判定更新时比较证据
    等级、按 ``derived_ids`` 补写复现事件与结论翻转时重新归因（判定更新链）。
    """

    outcome: str = OUTCOME_PARTIAL     # success / partial / failure
    evidence: str = EVIDENCE_MODEL_ONLY  # external / user_feedback / tool_error / model_only
    revision: int = 0                  # 修订号，每次更新加一
    judge_version: str = "0"           # 判策略版本
    model: str = ""                    # 判定所用的模型
    judged_by: str = JUDGED_BY_MODEL   # model / rule / external
    evidence_msgs: list[int] = field(default_factory=list)  # 依据所在的消息下标
    derived_ids: list[str] = field(default_factory=list)    # 本条轨迹产出/命中的经验 id
    coverage_gap: bool = False         # 失败且与已取用条目均无关（§3.2）
    judged_at: str = ""                # 判定时间，ISO 8601


def trajectory_expires_at() -> str:
    """从当前时刻起算的轨迹过期时间（ISO 8601）。"""
    return (datetime.now(UTC) + timedelta(seconds=TRAJECTORY_TTL_SECONDS)).isoformat()


# -- 轨迹记录读写 -------------------------------------------------------- #


class TrajectoryStore:
    """轨迹记录的读写：写入落点 scope 下的 ``/trajectories/`` 前缀键。

    与对话原文同一个 KV 存储（经 evolver 解析原文存储的同一规则取得实例），
    TTL 按 ``expires_at`` 剩余时长随每次写入重算。作者摘要（``digest``）由调用方
    （评价算子）用部署密钥算好传入——存取两侧用同一把密钥，键才对得上。
    """

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    def load(
        self, scope: Scope, digest: str, trajectory_id: str
    ) -> ExecutionTrajectoryRecord | None:
        """读一条轨迹记录；不存在或已过期返回 ``None``。"""
        try:
            raw = self._kv.get(scope, trajectory_key(digest, trajectory_id))
        except NotFoundError:
            return None
        return self._from_raw(raw)

    def save(
        self, scope: Scope, digest: str, record: ExecutionTrajectoryRecord
    ) -> None:
        """写入轨迹记录（新建或覆写状态），TTL 取 ``expires_at`` 剩余时长。"""
        key = trajectory_key(digest, record.trajectory_id)
        ttl = _ttl_until(record.expires_at)
        payload = encode_payload(asdict(record))
        try:
            self._kv.update(scope, key, payload, ttl=ttl)
        except NotFoundError:
            self._kv.insert(scope, key, payload, ttl=ttl)

    @staticmethod
    def _from_raw(raw: bytes) -> ExecutionTrajectoryRecord:
        data = decode_payload(raw)
        if not isinstance(data, dict):
            raise ValueError("trajectory record payload must be a JSON object")
        return ExecutionTrajectoryRecord(
            trajectory_id=str(data.get("trajectory_id", "")),
            user=_scope_from_raw(data.get("user")),
            producer=_scope_from_raw(data.get("producer")),
            credential_id=str(data.get("credential_id", "")),
            messages=list(data.get("messages", [])),
            capabilities=list(data.get("capabilities", [])),
            retrieved=(
                list(data["retrieved"]) if data.get("retrieved") is not None else None
            ),
            tool_call_count=int(data.get("tool_call_count", 0)),
            external_score=(
                float(data["external_score"])
                if data.get("external_score") is not None
                else None
            ),
            context=dict(data.get("context") or {}),
            status=str(data.get("status", STATUS_PROCESSING)),
            retries=int(data.get("retries", 0)),
            started_at=str(data.get("started_at", "")),
            expires_at=str(data.get("expires_at", "")),
        )


# -- 判定记录读写 -------------------------------------------------------- #


class VerdictStore:
    """判定记录的读写：与轨迹记录同一个写入落点、同期 30 天保留。"""

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    def load(
        self, scope: Scope, digest: str, trajectory_id: str
    ) -> VerdictRecord | None:
        """读一条判定记录；不存在返回 ``None``。"""
        try:
            raw = self._kv.get(scope, verdict_key(digest, trajectory_id))
        except NotFoundError:
            return None
        return self._from_raw(raw)

    def save(self, scope: Scope, digest: str, trajectory_id: str,
             record: VerdictRecord) -> None:
        """写入判定记录（初判与每次修订都是覆写）。"""
        key = verdict_key(digest, trajectory_id)
        payload = encode_payload(asdict(record))
        try:
            self._kv.update(scope, key, payload, ttl=TRAJECTORY_TTL_SECONDS)
        except NotFoundError:
            self._kv.insert(scope, key, payload, ttl=TRAJECTORY_TTL_SECONDS)

    @staticmethod
    def _from_raw(raw: bytes) -> VerdictRecord:
        data = decode_payload(raw)
        if not isinstance(data, dict):
            raise ValueError("verdict record payload must be a JSON object")
        return VerdictRecord(
            outcome=str(data.get("outcome", OUTCOME_PARTIAL)),
            evidence=str(data.get("evidence", EVIDENCE_MODEL_ONLY)),
            revision=int(data.get("revision", 0)),
            judge_version=str(data.get("judge_version", "0")),
            model=str(data.get("model", "")),
            judged_by=str(data.get("judged_by", JUDGED_BY_MODEL)),
            evidence_msgs=[int(i) for i in data.get("evidence_msgs", [])],
            derived_ids=[str(i) for i in data.get("derived_ids", [])],
            coverage_gap=bool(data.get("coverage_gap", False)),
            judged_at=str(data.get("judged_at", "")),
        )


# ====================================================================== #
# 报错规则（mem2.0 §3.1 证据等级表的 ``tool_error`` 级）
# ====================================================================== #
# 规则不调模型：轨迹中有报错（工具消息 ``is_error`` 为真）、且之后**同一调用**
# （工具名加主要参数）未再成功，才构成失败依据；报错已恢复（同一调用后来成功）
# 或无报错时该级无依据，判定转下一级。
#
# 例（§2.2 的 t-0001）：``python -m app`` 报 ``config/app.yaml`` 缺失，之后以
# ``cp config/app.example.yaml config/app.yaml && python -m app`` 启动成功——报错的
# 主要参数 ``python -m app`` 包含在后来成功的调用里，同一调用已成功，报错视为
# 已恢复，不判失败。

# 「主要参数」：工具调用 arguments 解析为 JSON 对象时取第一个键的值（多数工具
# 把主参数放在第一位，如 bash 的 command、read_file 的 path）；解析失败取原串。
# 识别「同一调用」时，报错调用的主要参数被后来成功调用的主要参数**包含**（前
# 者足够长时）或两者相等，都算同一调用——修复动作常常包裹原命令（先 cp 再重跑），
# 严格相等会把已恢复的报错误判为未恢复。
_CONTAINMENT_MIN_LEN = 8


def _primary_argument(arguments: object) -> str:
    """取一次工具调用的主要参数（见本节开头的取法说明）。"""
    raw = arguments if isinstance(arguments, str) else ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw.strip()
    if isinstance(payload, Mapping) and payload:
        first = next(iter(payload.values()))
        if isinstance(first, str):
            return first.strip()
        return json.dumps(first, ensure_ascii=False, sort_keys=True)
    return raw.strip()


def _tool_calls_by_id(messages: list[dict]) -> dict[str, tuple[str, str]]:
    """建 ``tool_call_id → (工具名, 主要参数)`` 索引，取自 assistant 消息的 tool_calls。"""
    index: dict[str, tuple[str, str]] = {}
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            call_id = str(call.get("id", "")).strip()
            function = call.get("function")
            if not call_id or not isinstance(function, Mapping):
                continue
            name = str(function.get("name", "")).strip()
            index[call_id] = (name, _primary_argument(function.get("arguments")))
    return index


def _is_tool_message(message: Mapping) -> bool:
    return str(message.get("role", "")) == "tool"


def _is_error(message: Mapping) -> bool:
    raw = message.get("is_error")
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def unresolved_tool_errors(messages: list[dict]) -> list[int]:
    """返回未恢复的报错所在消息下标（升序）；无未恢复报错返回空列表。

    一个报错「已恢复」当且仅当它之后存在一次成功的工具消息，其工具名相同、且
    主要参数与报错调用的主要参数相等或包含它（包含匹配要求报错侧主要参数长度
    不小于 :data:`_CONTAINMENT_MIN_LEN`，防止 ``ls`` 这类短命令误匹配一切）。
    """
    call_index = _tool_calls_by_id(messages)
    unresolved: list[int] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping) or not _is_tool_message(message):
            continue
        if not _is_error(message):
            continue
        call_id = str(message.get("tool_call_id", "")).strip()
        name, primary = call_index.get(call_id, ("", ""))
        recovered = False
        for later in messages[index + 1:]:
            if not isinstance(later, Mapping) or not _is_tool_message(later):
                continue
            if _is_error(later):
                continue
            later_id = str(later.get("tool_call_id", "")).strip()
            later_name, later_primary = call_index.get(later_id, ("", ""))
            if not name or later_name != name:
                continue
            if primary and primary == later_primary:
                recovered = True
                break
            if (
                len(primary) >= _CONTAINMENT_MIN_LEN
                and primary in later_primary
            ):
                recovered = True
                break
        if not recovered:
            unresolved.append(index)
    return unresolved


def tool_call_digest(messages: list[dict]) -> list[tuple[str, str]]:
    """轨迹消息 → 工具调用序列 ``[(工具名, 主要参数), ...]``（消息序）。

    技能归纳（SkillAbstractor，§2.5.3）用：生成步把选出轨迹的工具调用交给模型，
    命令覆盖检查在同一份序列里找步骤命令。只取 ``tool_calls`` 声明的
    调用，不含工具结果——命令是否执行过以调用为准。
    """
    return list(_tool_calls_by_id(messages).values())


def has_unresolved_error(messages: list[dict]) -> bool:
    """轨迹是否构成 ``tool_error`` 级失败依据（存在未恢复的报错）。"""
    return bool(unresolved_tool_errors(messages))


def has_error_mark(messages: list[dict]) -> bool:
    """轨迹中是否有报错（含已恢复的）。

    与 :func:`has_unresolved_error` 的区别：本函数服务于「值得提炼」的判定
    （§2.2 四条之一「轨迹中有报错」）——已恢复的报错同样值得提炼（t-0001 的
    配置缺失即属此类），只有成败判定才要求报错未恢复。
    """
    return any(
        isinstance(message, Mapping)
        and _is_tool_message(message)
        and _is_error(message)
        for message in messages
    )


# ====================================================================== #
# 轨迹消息的读取与压缩（mem2.0 §2.2）
# ====================================================================== #
# 评价算子（判定反馈、成败判定）与抽取算子（提炼、修正）都要从轨迹消息里找
# 「本次新输入」「最后一段结果」「执行过程压缩」这类结构，集中在本节：
#
# - 判定反馈：最后一条用户消息（对上一条轨迹的回应）、最后一条位于某次结果
#   之后的用户消息（轨迹内的多轮回应）
# - 判定成败 / 提炼：开头请求、最后一段结果、压缩后的执行过程

# 压缩轨迹的行数上限：判定与提炼的 prompt 都不需要完整原文。
_TRACE_LINE_LIMIT = 40

# 单行截断长度：压缩轨迹里每条消息只保留开头一段。
_TRACE_LINE_CHARS = 200


def is_role(message: object, role: str) -> bool:
    """消息是否为指定角色（非 dict 消息恒为否）。"""
    return isinstance(message, Mapping) and str(message.get("role", "")) == role


def last_user_message(messages: list[dict]) -> tuple[int, str] | None:
    """最后一条用户消息 ``(下标, 正文)``——本次新输入（§3.1 用户反馈的来源）。"""
    for index in range(len(messages) - 1, -1, -1):
        if is_role(messages[index], "user"):
            return index, str(messages[index].get("content", ""))
    return None


def first_user_message(messages: list[dict]) -> str:
    """开头请求：第一条用户消息正文；无则空串。"""
    for message in messages:
        if is_role(message, "user"):
            return str(message.get("content", ""))
    return ""


def _assistant_result(message: object) -> bool:
    """assistant 消息是否构成一次「结果」：有正文且不带工具调用。"""
    if not is_role(message, "assistant"):
        return False
    if isinstance(message, Mapping) and message.get("tool_calls"):
        return False
    return bool(str(message.get("content", "")).strip())


def last_assistant_reply(messages: list[dict]) -> str:
    """最后一条构成结果的 assistant 消息正文；无则空串。"""
    for message in reversed(messages):
        if _assistant_result(message):
            return str(message.get("content", ""))
    return ""


def last_post_result_user_message(messages: list[dict]) -> int | None:
    """最后一条「位于某次结果之后」的用户消息下标。

    多轮输入合为一条轨迹时才有（单轮轨迹的最后一条用户消息在所有结果之前）。
    """
    seen_result = False
    found: int | None = None
    for index, message in enumerate(messages):
        if _assistant_result(message):
            seen_result = True
        elif is_role(message, "user") and seen_result:
            found = index
    return found


def compress_trace(messages: list[dict]) -> str:
    """压缩执行过程供判定/归因/提炼 prompt 使用。

    每条消息一行（tool 消息保留报错标记与正文开头；带工具调用的 assistant 只
    留工具名列表），超出行数上限时保留首尾、中间折叠。只降篇幅，不改变结论
    依据（报错是否恢复等结构判定在报错规则一节里做，不用压缩结果）。
    """
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role", ""))
        content = str(message.get("content", "")).strip()
        if role == "assistant" and message.get("tool_calls"):
            names = [
                str(call.get("function", {}).get("name", ""))
                for call in message.get("tool_calls", [])
                if isinstance(call, Mapping)
            ]
            lines.append(f"assistant calls tools: {', '.join(names)}")
        elif role == "tool":
            name = str(message.get("name", ""))
            error = " (ERROR)" if has_error_mark([message]) else ""
            lines.append(f"tool {name}{error}: {content[:_TRACE_LINE_CHARS]}")
        else:
            lines.append(f"{role}: {content[:_TRACE_LINE_CHARS]}")
    limit = _TRACE_LINE_LIMIT
    if len(lines) > limit:
        head, tail = lines[: limit // 2], lines[-(limit - limit // 2):]
        lines = [*head, "...(omitted)...", *tail]
    return "\n".join(lines)


# ====================================================================== #
# 密钥清洗与脱敏检查（mem2.0 §2.1/§2.4）
# ====================================================================== #
# 两个确定性检查，服务两条不同的边界：
#
# - **密钥清洗**（:func:`scrub_messages`）：轨迹落盘**前**改写消息文本——把凭据
#   样式的取值（API key、token、密码赋值、Authorization 头）替换为占位符。轨迹
#   存的是作者自己的空间，清洗不是为了跨用户隔离，而是不让明文凭据进任何存储。
# - **脱敏检查**（:func:`desensitization_hit`）：经验条目写入共享空间**前**的丢弃
#   判定——条目正文命中与主体相关的取值（用户目录、账号名、主机名）即整条丢弃，
#   不改写。仓库内的相对路径（如 ``config/app.yaml``）、参数名与错误码可以保留。
#
# 两者都是确定性规则，不调模型；「经验数量每天数百条、不设事前人工审核」的
# 风险控制以它们为写入闸（§2.4 的「风险控制」）。

# -- 密钥清洗 ---------------------------------------------------------- #

_REDACTED = "[REDACTED]"

# 凭据样式的值：常见服务的 key/token 前缀（OpenAI sk-、GitHub ghp_、AWS AKIA、
# Slack xox、JWT eyJ）与足够长的十六进制/base62 串前缀明确者。
_CREDENTIAL_VALUE_RE = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")\b"
)

# 键名暗示凭据的 ``key=value`` / ``"key": "value"`` / ``key: value`` 赋值：值部分
# 整段替换。键名命中 password/passwd/secret/token/api_key/apikey/access_key/
# authorization/bearer。JSON 形态键名与冒号之间隔一个引号，分隔符组允许它。
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"authorization|bearer)\b([\"']?\s*[=:]\s*)(\"[^\"]{4,}\"|'[^']{4,}'|[^\s,;\"']{4,})"
)

# Authorization 头的值段（Bearer/Basic 后面的凭据）。
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization\s*:\s*(?:bearer|basic|token)\s+)([A-Za-z0-9._~+/-]{8,})"
)


def scrub_text(text: str) -> str:
    """清洗一段文本中的凭据：命中即替换为 ``[REDACTED]``，未命中原样返回。"""
    if not text:
        return text
    cleaned = _CREDENTIAL_VALUE_RE.sub(_REDACTED, text)
    cleaned = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + _REDACTED, cleaned)
    # 键值赋值只替换值段，保留键名（键名本身不是凭据，清洗后轨迹仍可读）。
    cleaned = _SECRET_ASSIGNMENT_RE.sub(
        lambda m: m.group(1) + m.group(2) + _REDACTED, cleaned
    )
    return cleaned


def _scrub_value(value: Any) -> Any:
    """递归清洗消息里的字符串值；非字符串原样返回。"""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    return value


def scrub_messages(messages: list[dict]) -> list[dict]:
    """轨迹落盘前清洗消息数组：返回清洗后的新列表，不改动入参。

    只清洗值，不动消息的角色、工具名等结构键；``tool_calls`` 的 ``arguments``
    是 JSON 字符串，作为字符串值一并清洗。
    """
    cleaned: list[dict] = []
    for message in messages:
        if not isinstance(message, Mapping):
            cleaned.append(message)
            continue
        cleaned.append(
            {str(key): _scrub_value(value) for key, value in message.items()}
        )
    return cleaned


# -- 脱敏检查 ---------------------------------------------------------- #

# 用户目录形态：/home/<名>、/Users/<名>、C:\Users\<名>——主体取值最常见的载体。
_HOME_DIR_RE = re.compile(
    r"(?:/home/|/Users/|[Cc]:[\\/]Users[\\/])([A-Za-z0-9._-]{2,})"
)

# 参与判定的主体标识最短长度：过短的标识（如 "u1"）作为子串误命中率高，但用户
# 目录形态里同样可能短——目录命中单独判，不套此长度。
_MIN_IDENTIFIER_LEN = 3


def subject_identifiers(*, user: str = "", agent: str = "", author: str = "") -> list[str]:
    """收集与主体相关的标识：scope 的 user/agent 维与作者主体中的名字段。

    ``author`` 形如 ``"user:u1"`` / ``"agent:a1"``，取冒号后的名字段。返回去重
    后的非空列表。
    """
    names: list[str] = []
    for candidate in (user, agent):
        name = str(candidate or "").strip()
        if name:
            names.append(name)
    principal = str(author or "").strip()
    if ":" in principal:
        name = principal.split(":", 1)[1].strip()
        if name:
            names.append(name)
    elif principal:
        names.append(principal)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def desensitization_hit(content: str, identifiers: list[str]) -> bool:
    """脱敏检查：经验条目正文命中主体相关取值即返回 ``True``（整条丢弃）。

    两类命中：

    - 主体标识作为独立词出现在正文中（词边界匹配，避免 ``u1`` 命中 ``u10``；
      短于 :data:`_MIN_IDENTIFIER_LEN` 的标识跳过——命中噪声大于信号）；
    - 用户目录形态 ``/home/<名>`` 等中的名字段与任一标识相同（目录命中不套
      长度下限，主体取值以目录形态出现本身就是命中信号）。

    仓库内相对路径（``config/app.yaml``）、参数名与错误码不含主体标识，不会命中。
    """
    if not content:
        return False
    for name in identifiers:
        name = str(name or "").strip()
        if not name:
            continue
        if len(name) >= _MIN_IDENTIFIER_LEN and re.search(
            rf"(?<![A-Za-z0-9._-]){re.escape(name)}(?![A-Za-z0-9._-])", content
        ):
            return True
    for match in _HOME_DIR_RE.finditer(content):
        if match.group(1) in identifiers:
            return True
    return False


# ====================================================================== #
# 作者摘要：作者主体经部署密钥做 HMAC 的结果（mem2.0 §2.1）
# ====================================================================== #
# 轨迹记录、判定记录的键与效用事件的事件标识都以作者摘要区分作者，键中不出现
# 作者本身。密钥是装配参数 ``experience_author_key``（登记在
# ``security/types.py`` 的 ``SECRET_PARAM_KEYS``，可经 ``${VAR}`` 从环境变量注入），
# 未配置时经验 profile 装配失败。
#
# 密钥不随加密密钥轮换、部署后固定不换：跨用户复现计数与来源轨迹的定位都以
# 「同一作者的摘要不变」为前提。摘要只用于拼键，不要求可逆。


def author_digest(author: str, key: str) -> str:
    """作者主体 → HMAC-SHA256 摘要（hex）。

    ``author`` 形如 ``"user:u1"`` / ``"agent:a1"``；``key`` 是部署密钥
    ``experience_author_key``，空串视为未配置（调用方装配期已校验非空）。
    """
    return hmac.new(
        key.encode("utf-8"), author.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def author_principal(*, user: str = "", agent: str = "") -> str:
    """从写入身份推导作者主体：有终端用户取 ``user:<用户>``，否则 ``agent:<agent>``。

    mem2.0 的「用户」指写入身份推导出的作者主体（§2.1）：终端用户经 agent 写入
    时为前者，agent 自主运行时为后者。
    """
    if user:
        return f"user:{user}"
    if agent:
        return f"agent:{agent}"
    return ""


# ====================================================================== #
# 部署配置的解析（mem2.0 §2）
# ====================================================================== #
# ``globals.experience`` 是经验沉淀的总开关：没有这一项时经验 profile 不应被
# 引用（写入侧被引擎的 profile 准入拒绝，§2.1 不回落）。本节把原始 mapping
# 解析成带缺省值的配置对象，形态校验集中一份——assembly（erasure 回调、缺省
# 落点鉴权）与 appraiser / extractor / curator / skill_abstractor 四个算子的
# 装配都从这取值，不各自再解析一遍。

# 经验条目的 ``memory_type`` 取值（v3 §2.3 的隔离类型之一）。
MEMORY_TYPE_EXPERIENCE = "experience"
# ``default_space`` 的写入落点取值：经验落在写入者自己的空间（§2.3）。
DEFAULT_SPACE_OWN = "own"
# 技能到达 agent 的方式（curated_spaces 每空间一项，缺省 manual）。
RELEASE_MANUAL = "manual"
RELEASE_AUTO = "auto"
# 复现计数的单位（curated_spaces 每空间一项，缺省 auto——经验落在写入者
# 自己的空间时按轨迹、其余按用户，§2.4）。
REPRO_UNIT_AUTO = "auto"
REPRO_UNIT_USER = "user"
REPRO_UNIT_TRAJECTORY = "trajectory"

_SKILL_RELEASE_VALUES = frozenset({RELEASE_MANUAL, RELEASE_AUTO})
_REPRO_UNIT_VALUES = frozenset({REPRO_UNIT_AUTO, REPRO_UNIT_USER, REPRO_UNIT_TRAJECTORY})


@dataclass(frozen=True)
class CuratedSpaceConfig:
    """``curated_spaces`` 一项的解析形态：一个周期整理空间的配置。"""

    space: str
    skill: bool = False
    skill_release: str = RELEASE_MANUAL
    repro_unit: str = REPRO_UNIT_AUTO


@dataclass(frozen=True)
class ExperienceConfig:
    """``globals.experience`` 的解析形态（mem2.0 §2「部署配置」）。

    - ``memory_class``：带 ``coords`` 的写入经验标为的判定表类别
    - ``default_space``：不带 ``coords`` 的落点——``"own"``（写入落点）或空间名
    - ``curated_spaces``：做周期整理的空间配置，可为空元组（不做周期整理）
    """

    memory_class: str = MEMORY_TYPE_EXPERIENCE
    default_space: str = DEFAULT_SPACE_OWN
    curated_spaces: tuple[CuratedSpaceConfig, ...] = ()

    @property
    def curated_space_names(self) -> frozenset[str]:
        """做周期整理的空间名集合。"""
        return frozenset(item.space for item in self.curated_spaces)

    @property
    def skill_space_names(self) -> frozenset[str]:
        """开启技能沉淀的空间名集合（``curated_spaces`` 中 ``skill`` 为真的）。"""
        return frozenset(item.space for item in self.curated_spaces if item.skill)

    def curated_of(self, space: str) -> CuratedSpaceConfig | None:
        """取一个空间的周期整理配置；未列入 ``curated_spaces`` 时返回 ``None``。"""
        for item in self.curated_spaces:
            if item.space == space:
                return item
        return None

    def experience_space_of(self, scope: Scope) -> str:
        """经验条目的落点空间名（§2.3）：``default_space`` 解析——``own`` 取
        写入落点（本次写入的 ``MemoryUnit`` 的 scope 所在空间），否则取配置
        的空间名。
        """
        if self.default_space == DEFAULT_SPACE_OWN:
            return scope.space or ""
        return self.default_space

    def resolved_repro_unit(self, space: str, naming: SpaceNaming | None) -> str:
        """该空间复现计数的最终口径（§2.4）：``auto`` 在装配期解析成两口径之一。

        ``auto`` 按落点选：经验落在写入者自己的空间（空间名由判定表 fallback
        类别的模板渲染，如 ``u_{user}`` 渲出的 ``u_alice``）按 ``trajectory``，
        其余按 ``user``。无判定表时经验不经落点判定、``default_space`` 即落点：
        ``own`` 的部署经验全落写入者空间，按 ``trajectory``。显式配置的
        ``user`` / ``trajectory`` 原样返回；未列入 ``curated_spaces`` 的空间
        都按 ``auto``（§2 部署配置）。
        """
        curated = self.curated_of(space)
        configured = curated.repro_unit if curated is not None else REPRO_UNIT_AUTO
        if configured != REPRO_UNIT_AUTO:
            return configured
        if naming is not None and naming.fallback_class:
            return (
                REPRO_UNIT_TRAJECTORY
                if naming.is_fallback_space(space)
                else REPRO_UNIT_USER
            )
        return (
            REPRO_UNIT_TRAJECTORY
            if self.default_space == DEFAULT_SPACE_OWN
            else REPRO_UNIT_USER
        )


def parse_experience_config(raw: Any) -> ExperienceConfig | None:
    """解析 ``globals.experience``；未配置（空值）返回 ``None``。

    未配置 = 经验沉淀关闭，不是坏配置。已配置但形态不对（非映射、
    ``curated_spaces`` 非列表、项缺空间名或重复、``skill_release`` /
    ``repro_unit`` 取值域外）抛 :class:`ValidationError`，经验 profile 装配
    失败。
    """
    if not raw:
        return None
    if not isinstance(raw, Mapping):
        raise ValidationError(
            f"globals.experience must be a mapping, got {type(raw).__name__}"
        )
    memory_class = str(raw.get("memory_class", "")).strip() or MEMORY_TYPE_EXPERIENCE
    default_space = str(raw.get("default_space", "")).strip() or DEFAULT_SPACE_OWN
    raw_curated = raw.get("curated_spaces")
    if raw_curated is None:
        raw_curated = ()
    if not isinstance(raw_curated, (list, tuple)):
        raise ValidationError(
            "globals.experience.curated_spaces must be a list, "
            f"got {type(raw_curated).__name__}"
        )
    curated: list[CuratedSpaceConfig] = []
    seen: set[str] = set()
    for item in raw_curated:
        if not isinstance(item, Mapping):
            raise ValidationError(
                "globals.experience.curated_spaces 的每一项须是映射"
                "（space / skill / skill_release / repro_unit）"
            )
        space = str(item.get("space", "")).strip()
        if not space:
            raise ValidationError(
                "globals.experience.curated_spaces 的每一项须声明 space（空间名）"
            )
        if space in seen:
            raise ValidationError(
                f"globals.experience.curated_spaces 空间名重复：{space!r}"
            )
        seen.add(space)
        release = str(item.get("skill_release", "")).strip() or RELEASE_MANUAL
        if release not in _SKILL_RELEASE_VALUES:
            raise ValidationError(
                f"curated_spaces[{space!r}].skill_release 须是 "
                f"{sorted(_SKILL_RELEASE_VALUES)} 之一，got {release!r}"
            )
        repro = str(item.get("repro_unit", "")).strip() or REPRO_UNIT_AUTO
        if repro not in _REPRO_UNIT_VALUES:
            raise ValidationError(
                f"curated_spaces[{space!r}].repro_unit 须是 "
                f"{sorted(_REPRO_UNIT_VALUES)} 之一，got {repro!r}"
            )
        curated.append(
            CuratedSpaceConfig(
                space=space,
                skill=bool(item.get("skill", False)),
                skill_release=release,
                repro_unit=repro,
            )
        )
    return ExperienceConfig(
        memory_class=memory_class,
        default_space=default_space,
        curated_spaces=tuple(curated),
    )


def space_naming_of(config: Any) -> SpaceNaming | None:
    """从装配配置读判定表的命名规则（``globals.route_table``）。

    经验算子装配期用它解析 ``repro_unit: auto`` 的空间口径（§2.4）；未配置
    判定表（未开启落点判定）返回 ``None``，解析退到 ``default_space``。
    """
    from jiuwen_memory.construction.router import parse_route_table

    raw = config.get("route_table") if hasattr(config, "get") else None
    if not raw or not isinstance(raw, Mapping):
        return None
    return parse_route_table(raw).naming


def make_erasure_callback(
    utility: UtilityStore,
    *,
    author_key: str,
    curated_spaces: Sequence[str],
) -> Callable[[str, str, list[Scope]], None]:
    """构造按作者清除的登记回调（§2.5.2「按作者清除」，装配注入空间删除流程）。

    回调在用户空间删除完成后被调用：由被删空间的归属主体得到作者主体，用
    部署密钥算出作者摘要，在该组织的每个周期整理空间写清除标记
    ``/erased-authors/{摘要}``。清除本身由下一轮周期整理在聚合之前执行
    （删除这些作者的效用事件、退休只由他复现过的条目），需要立即清除时手动
    执行一轮 evolve。

    ``author_key`` 必须与评价算子的 ``author_key`` 同值——摘要对不上则标记
    永远匹配不到任何事件，清除静默失效（装配层从同一份配置取值保证）。
    ``utility`` 与经验空间的评价/整理算子按同一条 ``_resolve_message_store``
    规则取实例（§2.1「轨迹记录所用的 KV 存储」）。
    """
    spaces = tuple(curated_spaces)

    def on_space_deleted(org: str, space: str, owners: list[Scope]) -> None:
        del space  # 被删空间名不参与登记——标记写在周期整理空间，不在被删空间
        for owner in owners:
            author = author_principal(user=owner.user, agent=owner.agent)
            if not author:
                continue
            digest = author_digest(author, author_key)
            for exp_space in spaces:
                utility.add_erased_author(Scope(org=org, space=exp_space), digest)

    return on_space_deleted


# ====================================================================== #
# 经验的评价结论（mem2.0 §2.2）
# ====================================================================== #
# 评价算子（ExperienceAppraiser）写，抽取算子（ExperienceExtractor）与 ``finalize``
# 读；经 :class:`~construction.appraiser.Appraisal` 的 ``data`` 字段传递，再由
# evolver 挂到 ``ExtractContext.appraisal`` 上。


@dataclass
class ExperienceAppraisal:
    """一次轨迹评价的全部结论。"""

    record: ExecutionTrajectoryRecord  # 清洗后的轨迹记录（§2.1）
    verdict: VerdictRecord             # 成败判定（§3.1）
    attribution: dict[str, str] = field(default_factory=dict)
    # 取用过的条目 id → helpful / harmful / neutral
    worth_extracting: bool = False     # 轨迹是否值得提炼（§2.2 的四条之一）


# ====================================================================== #
# 危险操作的规则清单（mem2.0 §2.5.5，技能自动发布的附加条件）
# ====================================================================== #
# 自动发布（skill_release=auto）在草稿写入前多做一步确定性检查：步骤命中
# 任一模式即不自动发布、退回草稿等人审。清单是首版固定取值，读取方只有
# SkillAbstractor；改清单等于改判策略，同步更新 F10。


# 每项：(模式名, 正则)——匹配对象是 SKILL.md 全文（步骤小节为主，全文兜底）。
RISKY_OPERATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 关闭证书或签名校验：kubectl/insecure/verify=false/noverify 等
    (
        "disable-verification",
        re.compile(
            r"--insecure(-tls)?\b|--no-verify\b|verify(ssl|_certs)?\s*[=:]\s*(false|no)|"
            r"skip[-_ ]tls|ssl[-_ ]?verify\s*[=:]\s*(false|no)|noverify",
            re.IGNORECASE,
        ),
    ),
    # 递归删除：rm -rf / rm -fr 及 docker system prune 之类通配清理
    (
        "recursive-delete",
        re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b|system\s+prune", re.IGNORECASE),
    ),
    # 放开全部权限：chmod/chown 777、--privileged
    (
        "grant-all-permissions",
        re.compile(r"\bchmod\s+(-R\s+)?777\b|chown\s+(-R\s+)?777\b|--privileged\b", re.IGNORECASE),
    ),
    # 把下载的内容直接交给解释器执行：curl/wget 管道到 bash/sh/python
    (
        "pipe-download-to-interpreter",
        re.compile(
            r"(curl|wget)[^|;\n]*\|\s*(sudo\s+)?(ba)?sh\b|"
            r"(curl|wget)[^|;\n]*\|\s*(python|python3|node)\b",
            re.IGNORECASE,
        ),
    ),
    # 写入或输出凭据：往凭据文件/环境写密钥、或把密钥打印/上传
    (
        "credential-exfiltration",
        re.compile(
            r">\s*~?/?\.?(ssh/.*(id_rsa|id_ed25519|authorized_keys)|\.netrc|\.aws/credentials)|"
            r"\b(cat|echo|print|puts)\b[^|;\n]*\b(api[_-]?key|secret|token|password)\s*[:=]",
            re.IGNORECASE,
        ),
    ),
)


def risky_operation_hit(content: str) -> str | None:
    """命中危险操作清单时返回模式名（未命中返回 ``None``）。

    匹配对象是 SKILL.md 全文：步骤之外的节（如「常见失败与处置」）同样
    不允许出现危险操作——它们也会被照着执行。命中只影响自动发布（退回
    草稿），不丢弃条目：人审可以对内容做出判断。
    """
    if not content:
        return None
    for name, pattern in RISKY_OPERATION_PATTERNS:
        if pattern.search(content):
            return name
    return None
