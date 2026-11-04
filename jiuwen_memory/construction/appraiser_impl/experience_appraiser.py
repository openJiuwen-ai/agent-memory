# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ExperienceAppraiser — 经验记忆的评价算子（mem2.0 §2.2 八步中的 1-5 与 8）。

``appraise`` 在抽取之前执行轨迹沉淀的第 1-5 步：重复检查（含外部结果补报）、
结构过滤、判定反馈（回填上一条轨迹的判定）、判定成败（写判定记录）、归因（写
归因事件）。``finalize`` 在流程结束时执行第 8 步收尾：对新增与判为重复命中的
条目写复现事件，更新复现作者记录与类别计数，把条目 id 记入判定记录，轨迹置为
已完成。

轨迹记录与判定记录存写入落点（与对话原文同一个 KV、同一个 scope）；效用事件
与小记录存经验空间。作者摘要用部署密钥 ``experience_author_key`` 做 HMAC，键中
不出现作者本身。

各情形下 ``finalize`` 的处理见 §2.2 的评价算子情形表：``result`` 为 ``None``
（流程抛异常）置处理失败；收尾自身出错时抛出前置处理失败；已处理完的轨迹再次
写入只按补报更新判定，请求不产出条目。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import (
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.experience import (
    ATTR_HARMFUL,
    ATTR_HELPFUL,
    ATTR_NEUTRAL,
    EVIDENCE_EXTERNAL,
    EVIDENCE_MODEL_ONLY,
    EVIDENCE_TOOL_ERROR,
    EVIDENCE_USER_FEEDBACK,
    OUTCOME_FAILURE,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    AttributionEvent,
    ReproductionEvent,
    UtilityStore,
    now_iso,
    parse_usage_ref,
)
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger, redact_for_log
from jiuwen_memory.common.security.principal import same_dims
from jiuwen_memory.common.type_def import (
    MEMORY_KEY_PREFIX,
    PRODUCER_CREDENTIAL_KEY,
    PRODUCER_PRINCIPAL_KEY,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.common.type_def.memory import LifecycleState, memory_key
from jiuwen_memory.common.type_def.memory_codec import loads as load_unit
from jiuwen_memory.construction.appraiser import (
    Appraisal,
    Appraiser,
    AppraiserProducer,
)
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.common import (
    resolve_message_store,
    validate_llm_attempt_policy,
)
from jiuwen_memory.construction.experience import (
    EVIDENCE_RANK,
    JUDGED_BY_EXTERNAL,
    JUDGED_BY_MODEL,
    JUDGED_BY_RULE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PROCESSING,
    ExecutionTrajectoryRecord,
    ExperienceAppraisal,
    ExperienceConfig,
    TrajectoryStore,
    VerdictRecord,
    VerdictStore,
    author_digest,
    author_principal,
    compress_trace,
    first_user_message,
    has_error_mark,
    last_assistant_reply,
    last_post_result_user_message,
    last_user_message,
    scrub_messages,
    tool_call_digest,
    trajectory_expires_at,
    unresolved_tool_errors,
)
from jiuwen_memory.construction.prompt_registry import (
    PHASE_APPRAISE,
    PromptRegistry,
)
from jiuwen_memory.storage.kv import KVStore

logger = get_logger(__name__)

# 值得提炼的工具调用次数阈值（§2.2「值得提炼」四条之一），首版 5。
DEFAULT_TOOL_CALL_THRESHOLD = 5
# 一次沉淀的处理时限（秒），各步开始前检查已用时间（§2.2「耗时与重试」）。
DEFAULT_PROCESSING_TIME_LIMIT = 150.0
# external_score 判成功的阈值缺省（按接入方配置覆盖，§3.1）。
DEFAULT_EXTERNAL_THRESHOLD = 0.999

# 加载技能的工具名（§2.5.7「识别」）：agent-core 的加载工具缺省叫
# skill_tool，接入方不同名时可经部署配置覆盖。技能归因从这条调用里识别。
SKILL_TOOL_NAME = "skill_tool"
# 技能条目的标记与已发布标签（§2.5.4；与 skill_abstractor / experience_
# curator 同款语义，各实现模块自持副本，不跨模块 import 私有常量）。
MEMORY_TYPE_SKILL = "skill"
SKILL_PUBLISHED_TAGS = ("approved", "trial")


# ====================================================================== #
# 提示词：登记在 yml ``prompts.appraise`` 段（feedback / outcome / attribute），
# 经 PromptRegistry 读取、可运行时替换；未配置时用本模块的内置文本。
# ====================================================================== #

_FEEDBACK_PROMPT = """\
You judge how a user responded to an agent's task result.

You will be given one or two pairs of (result, response). Each response is the
user's next input after that result. For each pair decide the user's stance:

- "correction": the response corrects the agent's understanding, steps or
  output (the result was wrong or unacceptable);
- "approval": the response clearly accepts the result;
- "none": neither (new instructions, follow-up questions, small talk, or
  ambiguous).

Output ONLY a JSON object, no markdown fences:
{"previous": "correction" | "approval" | "none",
 "internal": "correction" | "approval" | "none"}
Use "none" for a pair that was not given.
"""

_OUTCOME_PROMPT = """\
You judge whether an agent task succeeded.

You will be given the task's opening request (the first user input), a
compressed trace of the execution (tool calls and their results, including
errors), and the agent's final reply. Decide the outcome:

- "success": the request was fulfilled;
- "partial": part of the request was fulfilled, or the result is usable but
  incomplete;
- "failure": the request was not fulfilled.

Output ONLY a JSON object, no markdown fences:
{"outcome": "success" | "partial" | "failure"}
"""

_ATTRIBUTE_PROMPT = """\
You judge whether memory entries an agent retrieved during a task were useful.

You will be given the task's execution trace (tool calls and results,
including errors), the task's final outcome, and the entries that were
retrieved before the task. For each entry decide:

- "helpful": following the entry's advice contributed to the outcome;
- "harmful": following the entry's advice made things worse (e.g. the same
  error persisted after applying it);
- "neutral": the entry was retrieved but not applied, or had no bearing on
  the outcome.

If the task failed, also decide "unrelated_failure": true when the failure
was caused by something the retrieved entries did not cover (the right entry
was missing or never retrieved), false when a retrieved entry was involved in
the failure.

Output ONLY a JSON object, no markdown fences:
{"attributions": {"<entry id>": "helpful" | "harmful" | "neutral", ...},
 "unrelated_failure": true | false}
"""


def _prompt(registry: PromptRegistry, key: str, fallback: str) -> str:
    text = registry.get(PHASE_APPRAISE, key)
    return text if text else fallback


@dataclass
class _Feedback:
    """判定反馈的结论（§2.2 第 3 步）。"""

    previous: str = "none"   # 本次新输入对上一条轨迹：correction / approval / none
    internal: str = "none"   # 轨迹内最后一段结果之后的用户消息：同上
    internal_msg_index: int = -1  # internal 结论依据的消息下标（evidence_msgs 用）


class ExperienceAppraiser(Appraiser):
    """经验记忆的评价算子：轨迹沉淀八步中的第 1-5 步与第 8 步。"""

    def __init__(
        self,
        *,
        llm: LLM,
        kv: KVStore,
        author_key: str,
        experience: ExperienceConfig,
        prompt_registry: PromptRegistry | None = None,
        external_thresholds: dict[str, float] | None = None,
        tool_call_threshold: int = DEFAULT_TOOL_CALL_THRESHOLD,
        skill_tool_name: str = SKILL_TOOL_NAME,
        processing_time_limit: float = DEFAULT_PROCESSING_TIME_LIMIT,
        max_attempts: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        if not author_key:
            raise ValidationError(
                "experience_author_key 未配置：作者摘要的 HMAC 密钥缺失，"
                "经验 profile 装配失败（可经 ${VAR} 从环境变量注入）"
            )
        self._llm = llm
        self._kv = kv
        self._author_key = author_key
        # 经验配置（v3 §2）：落点解析（default_space）与技能沉淀开关
        # （curated_spaces 中 skill 为真的空间）都从这一份取。
        self._experience = experience
        self._prompts = prompt_registry or PromptRegistry()
        self._external_thresholds = dict(external_thresholds or {})
        self._tool_call_threshold = max(1, int(tool_call_threshold))
        # 加载技能的工具名（§2.5.7「识别」）：技能归因从这条调用里识别。
        self._skill_tool_name = str(skill_tool_name or "").strip() or SKILL_TOOL_NAME
        self._time_limit = float(processing_time_limit)
        self._max_attempts, self._retry_backoff_ms = validate_llm_attempt_policy(
            max_attempts, retry_backoff_ms,
        )
        self._trajectories = TrajectoryStore(kv)
        self._verdicts = VerdictStore(kv)
        self._utility = UtilityStore(kv)
        # 本条轨迹的处理起点（epoch 秒）；各步开始前据此检查处理时限。
        self._deadline_started: float | None = None

    # -- 契约 ---------------------------------------------------------------- #

    def operator_type(self) -> OperatorType:
        """返回本算子的类型 ``APPRAISER``。"""
        return OperatorType.APPRAISER

    def health(self) -> None:
        """探活：检查 LLM。KV 的健康由存储层自己检查。"""
        self._llm.health()

    # -- 第 1-5 步：appraise ------------------------------------------------- #

    def appraise(self, units: list[MemoryUnit]) -> Appraisal:
        """评价本次写入：准入、解析轨迹、判定成败、归因。"""
        if not units:
            raise ValidationError("experience 写入缺少内容单元")
        unit = units[0]
        self._deadline_started = time.monotonic()
        # 落点与写权已在接口层校验（§2.1）。
        scope = unit.scope
        meta = dict(unit.system_metadata or {})
        # 1 重复检查：解析请求概要字段（正文是否为轨迹 JSON 视记录状态而定）。
        request = _parse_request_fields(meta)
        author = author_principal(user=scope.user, agent=scope.agent)
        if not author:
            raise ValidationError(
                "experience 写入无法从 scope 推导作者主体（user/agent 均为空）"
            )
        digest = author_digest(author, self._author_key)

        existing = self._trajectories.load(scope, digest, request.trajectory_id)
        if existing is not None and existing.status == STATUS_DONE:
            # 已处理完：不作为新轨迹处理；带 external_score 时按补报更新判定。
            if request.external_score is not None:
                self._apply_external_supplement(scope, digest, existing, request)
            return Appraisal(proceed=False)
        if existing is not None and existing.status == STATUS_PROCESSING:
            if not self._processing_expired(existing):
                # 正在处理：不并行执行，返回可重试的错误。
                raise ConflictError(
                    entity="trajectory",
                    key=request.trajectory_id,
                    message="trajectory is being processed, retry later",
                )
        record = self._build_record(scope, request, meta, unit, existing)
        record.status = STATUS_PROCESSING
        record.started_at = now_iso()
        self._trajectories.save(scope, digest, record)
        try:
            return self._appraise_steps(scope, digest, record, request)
        except Exception:
            record.status = STATUS_FAILED
            self._trajectories.save(scope, digest, record)
            raise

    def _appraise_steps(
        self, scope: Scope, digest: str, record: ExecutionTrajectoryRecord,
        request: _TrajectoryRequest,
    ) -> Appraisal:
        # 2 结构过滤：三类信号都没有时无判定可做，轨迹置已完成、不再处理。
        if (
            record.tool_call_count == 0
            and not record.retrieved
            and not request.previous_trajectory_id
        ):
            record.status = STATUS_DONE
            self._trajectories.save(scope, digest, record)
            return Appraisal(proceed=False)

        # 3 判定反馈：模型判用户对结果的回应（0-1 次调用），回填上一条轨迹的判定。
        self._check_deadline("feedback")
        feedback = self._judge_feedback(scope, digest, record, request)

        # 4 判定成败：按证据等级表判定并写判定记录（0-1 次调用）。
        self._check_deadline("outcome")
        verdict = self._judge_outcome(record, feedback)
        self._verdicts.save(scope, digest, record.trajectory_id, verdict)

        # 5 归因：逐条判定取用过的条目并写归因事件（0-1 次调用）。
        self._check_deadline("attribute")
        attribution = self._attribute(scope, record, verdict, digest)

        worth = self._worth_extracting(record, feedback)
        return Appraisal(
            proceed=True,
            data=ExperienceAppraisal(
                record=record,
                verdict=verdict,
                attribution=attribution,
                worth_extracting=worth,
            ),
        )

    # -- 第 1 步的部件 -------------------------------------------------------- #

    def _build_record(
        self,
        scope: Scope,
        request: _TrajectoryRequest,
        meta: dict[str, Any],
        unit: MemoryUnit,
        existing: ExecutionTrajectoryRecord | None,
    ) -> ExecutionTrajectoryRecord:
        """解析正文为轨迹 JSON、清洗密钥，构造（或重试时复用）轨迹记录。

        轨迹记录不存在时正文必须是轨迹 JSON，否则参数错误（§3.1 补报的三处
        要求之「轨迹已处理完时正文不解析」在此之外）。重试时保留原 ``expires_at``
        与 ``retries`` 计数（重试次数在进入处理前置好）。

        归属主体 ``user`` 取请求 ``scope`` 的四维（space 是落点不是归属）；
        生产者 ``producer`` 从接口层写入的内核保留键与 ``scope.org`` 组出
        （§2.1「生产者与归属主体」）。作者不落盘：由 ``user`` 现推，键的作者
        摘要（``digest``）在存取两侧用同一把部署密钥现算。
        """
        payload = _parse_trajectory_payload(unit.content)
        messages = scrub_messages(payload.get("messages"))
        _validate_messages(messages)
        capabilities = payload.get("capabilities") or []
        retrieved = _normalize_retrieved(payload.get("retrieved"), request)
        context = _validate_context(payload.get("context"))
        owner = Scope(
            org=scope.org,
            user=scope.user,
            agent=scope.agent,
            session=scope.session,
        )
        producer = _producer_scope_of(request.producer_principal, scope.org)
        if existing is not None:
            record = existing
            record.user = owner
            record.producer = producer
            record.credential_id = request.credential_id
            record.messages = messages
            record.capabilities = capabilities
            record.retrieved = retrieved
            record.tool_call_count = request.tool_call_count
            record.external_score = request.external_score
            record.context = context
            record.retries = existing.retries + 1
            if not record.expires_at:
                record.expires_at = trajectory_expires_at()
            return record
        return ExecutionTrajectoryRecord(
            trajectory_id=request.trajectory_id,
            user=owner,
            producer=producer,
            credential_id=request.credential_id,
            messages=messages,
            capabilities=capabilities,
            retrieved=retrieved,
            tool_call_count=request.tool_call_count,
            external_score=request.external_score,
            context=context,
            status=STATUS_PROCESSING,
            retries=0,
            started_at=now_iso(),
            expires_at=trajectory_expires_at(),
        )

    @staticmethod
    def _processing_expired(record: ExecutionTrajectoryRecord) -> bool:
        """处理中的记录是否已超处理时限（进程中途退出的识别，§2.2）。"""
        try:
            from datetime import UTC, datetime

            started = datetime.fromisoformat(record.started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            return True
        elapsed = (datetime.now(UTC) - started).total_seconds()
        return elapsed > DEFAULT_PROCESSING_TIME_LIMIT

    def _apply_external_supplement(
        self,
        scope: Scope,
        digest: str,
        record: ExecutionTrajectoryRecord,
        request: _TrajectoryRequest,
    ) -> None:
        """补报事后外部结果：把判定更新为 external 等级（§3.1）。

        生产者须与写入该轨迹时是同一主体（``same_dims`` 逐维比对）：外部结果
        决定证据等级最高的结论，只接受发送这条轨迹的接入方补报，其他主体补报
        返回权限错误。证据等级更高的取代原结论；external 恒为最高级。同级补报
        （再次补报）只有结论变化时才更新。判定更新后的补做（按新结论补写复现、
        结论翻转时重新归因）见 :meth:`_post_verdict_update`（§3.1 判定更新链）。
        """
        producer = _producer_scope_of(request.producer_principal, scope.org)
        if not same_dims(record.producer, producer):
            raise PermissionDeniedError(
                "external supplement requires the same producer as the trajectory"
            )
        verdict = self._verdicts.load(scope, digest, record.trajectory_id)
        if verdict is None:
            logger.warning(
                "ExperienceAppraiser: supplement without verdict, trajectory=%s",
                record.trajectory_id,
            )
            return
        threshold = self._external_thresholds.get(
            record.producer_principal, DEFAULT_EXTERNAL_THRESHOLD
        )
        score = float(request.external_score)
        if score >= threshold:
            outcome = OUTCOME_SUCCESS
        elif score > 0:
            outcome = OUTCOME_PARTIAL
        else:
            outcome = OUTCOME_FAILURE
        old_rank = EVIDENCE_RANK.get(verdict.evidence, 0)
        new_rank = EVIDENCE_RANK[EVIDENCE_EXTERNAL]
        if new_rank < old_rank or (new_rank == old_rank and outcome == verdict.outcome):
            return
        old_outcome = verdict.outcome
        verdict.outcome = outcome
        verdict.evidence = EVIDENCE_EXTERNAL
        verdict.revision += 1
        verdict.judged_by = JUDGED_BY_EXTERNAL
        verdict.judged_at = now_iso()
        self._verdicts.save(scope, digest, record.trajectory_id, verdict)
        logger.info(
            "ExperienceAppraiser: verdict supplemented to outcome=%s, trajectory=%s",
            outcome,
            record.trajectory_id,
        )
        # 判定更新后的补做（§3.1）：轨迹记录在手（existing 刚从存储读出）。
        self._post_verdict_update(
            scope, digest, record.trajectory_id, record, verdict, old_outcome
        )

    # -- 第 3 步：判定反馈 ---------------------------------------------------- #

    def _judge_feedback(
        self,
        scope: Scope,
        digest: str,
        record: ExecutionTrajectoryRecord,
        request: _TrajectoryRequest,
    ) -> _Feedback:
        """模型判用户对结果的回应；对上一条轨迹的结论回填其判定记录。

        至多一次模型调用：上一条轨迹的最终回复 + 本次新输入、轨迹内最后一段
        结果之后的用户消息 + 该结果，两对一起判。两对都不存在时不调模型。
        反馈回填有会话核对（§3.1「判定反馈」）：两条轨迹不在同一会话、或带了
        ``context.turn`` 而上一条的轮次不更小时不回填，记一条警告日志——判定
        反馈只认同一会话里紧接着的回应，跨会话的输入不是对上一条结果的反馈。
        Pair P 的模型判定不受核对影响：新输入的态度仍是本条轨迹成败判定的输入。
        """
        feedback = _Feedback()
        new_input = last_user_message(record.messages)
        previous_pair = None
        conversation_continues = False
        if request.previous_trajectory_id and new_input is not None:
            previous = self._trajectories.load(
                scope, digest, request.previous_trajectory_id
            )
            conversation_continues = (
                previous is not None and self._same_conversation(record, previous)
            )
            previous_reply = last_assistant_reply(previous.messages) if previous else None
            if previous_reply:
                previous_pair = (previous_reply, new_input[1])
        internal_pair = None
        internal_index = last_post_result_user_message(record.messages)
        if internal_index is not None:
            result = last_assistant_reply(record.messages[:internal_index])
            if result:
                internal_pair = (result, str(record.messages[internal_index].get("content", "")))
                feedback.internal_msg_index = internal_index
        if previous_pair is None and internal_pair is None:
            return feedback
        parts: list[str] = []
        if previous_pair:
            parts.append(
                "## Pair P (response to the PREVIOUS task's result)\n"
                f"Result:\n{previous_pair[0]}\n\nResponse (this task's opening input):\n"
                f"{previous_pair[1]}"
            )
        if internal_pair:
            parts.append(
                "## Pair I (response to a result inside THIS task)\n"
                f"Result:\n{internal_pair[0]}\n\nResponse:\n{internal_pair[1]}"
            )
        prompt = _prompt(self._prompts, "feedback", _FEEDBACK_PROMPT)
        payload = self._call_llm(prompt, "\n\n".join(parts))
        feedback.previous = _parse_stance(payload.get("previous"))
        feedback.internal = _parse_stance(payload.get("internal"))
        if previous_pair is not None:
            if conversation_continues:
                self._backfill_previous_verdict(
                    scope, digest, request.previous_trajectory_id, feedback.previous
                )
            else:
                logger.warning(
                    "ExperienceAppraiser: feedback not in the same conversation, "
                    "skip backfill, trajectory=%s previous=%s",
                    record.trajectory_id,
                    request.previous_trajectory_id,
                )
        return feedback

    @staticmethod
    def _same_conversation(
        current: ExecutionTrajectoryRecord, previous: ExecutionTrajectoryRecord
    ) -> bool:
        """反馈会话核对（§3.1「判定反馈」）：同一会话且轮次递增。

        会话取归属主体的 ``session`` 维（与对话写入同一维）；轮次取背景信息
        ``context.turn``——本条带了 ``turn`` 时上一条也须带且更小，否则无从
        核对按不满足处理（保守不回填）。
        """
        if current.user.session != previous.user.session:
            return False
        turn = current.context.get("turn")
        if turn is None:
            return True
        previous_turn = previous.context.get("turn")
        if isinstance(previous_turn, bool) or not isinstance(previous_turn, int):
            return False
        return previous_turn < turn

    def _backfill_previous_verdict(
        self, scope: Scope, digest: str, previous_id: str, stance: str
    ) -> None:
        """把用户反馈回填到上一条轨迹的判定（§3.1：等级更高者取代原结论）。"""
        if stance not in {"correction", "approval"}:
            return
        verdict = self._verdicts.load(scope, digest, previous_id)
        if verdict is None:
            return
        if EVIDENCE_RANK[EVIDENCE_USER_FEEDBACK] <= EVIDENCE_RANK.get(verdict.evidence, 0):
            return
        old_outcome = verdict.outcome
        verdict.outcome = OUTCOME_FAILURE if stance == "correction" else OUTCOME_SUCCESS
        verdict.evidence = EVIDENCE_USER_FEEDBACK
        verdict.revision += 1
        verdict.judged_by = JUDGED_BY_MODEL
        verdict.judged_at = now_iso()
        self._verdicts.save(scope, digest, previous_id, verdict)
        logger.info(
            "ExperienceAppraiser: previous verdict backfilled by user feedback, "
            "trajectory=%s outcome=%s",
            previous_id,
            verdict.outcome,
        )
        # 判定更新后的补做（§3.1）：重新归因需要上一条轨迹的消息与使用记录。
        previous = self._trajectories.load(scope, digest, previous_id)
        self._post_verdict_update(
            scope, digest, previous_id, previous, verdict, old_outcome
        )

    # -- 判定更新后的补做（判定更新链，§3.1）---------------------------------- #

    def _post_verdict_update(
        self,
        scope: Scope,
        digest: str,
        trajectory_id: str,
        record: ExecutionTrajectoryRecord | None,
        verdict: VerdictRecord,
        old_outcome: str,
    ) -> None:
        """判定更新后的补做（§3.1 判定更新链）：按新结论补写复现事件；
        结论翻转时对取用过的条目重新归因。

        - 复现事件按判定修订号入键：新结论是新一条事件（``outcome`` /
          ``evidence`` 取新判定），聚合只取每条轨迹最新一轮（§3.2），统计随
          下一轮周期整理修正。证据等级单独升级（结论未翻转）也补写——升为
          技能只认 ``external`` / ``user_feedback`` 级的成功（§2.5.3）。
        - 结论翻转（``old_outcome != verdict.outcome``）时重新归因：复用第 5
          步（含技能识别），归因事件同样带新修订号。已产出的条目不撤回，由
          它们自身的使用效果决定去留。
        - 轨迹记录不在（30 天过期、或反馈回填时上一条已删除）时整体跳过：
          两类事件的内容字段都来自轨迹记录（``source_scope`` /
          ``retrieved_ids`` / 消息）。
        - 补做失败只记 warning：判定已更新，补报/反馈请求不因此失败；统计
          修正由下一次更高等级证据的更新再触发（新修订号新键）。
        """
        try:
            if record is None:
                logger.warning(
                    "ExperienceAppraiser: trajectory record gone, skip "
                    "post-verdict update, trajectory=%s",
                    trajectory_id,
                )
                return
            self._check_deadline("post-update")
            exp_scope = self._experience_scope_of(scope)
            source_scope = [
                scope.org, scope.space, scope.user, scope.agent, scope.session,
            ]
            retrieved_ids = [
                str(item.get("id", "")) for item in (record.retrieved or [])
            ]
            # 1 补写复现事件：derived_ids 按新结论各写一条（键含新修订号，幂等）。
            for entry_id in verdict.derived_ids:
                self._utility.write_reproduction_event(
                    exp_scope,
                    entry_id,
                    digest,
                    trajectory_id,
                    verdict.revision,
                    ReproductionEvent(
                        source_scope=source_scope,
                        trajectory_id=trajectory_id,
                        author=digest,
                        retrieved_ids=retrieved_ids,
                        outcome=verdict.outcome,
                        evidence=verdict.evidence,
                        occurred_at=now_iso(),
                    ),
                )
            # 2 结论翻转时重新归因：无使用记录且无技能时第 5 步内部短路。
            if old_outcome != verdict.outcome:
                self._attribute(scope, record, verdict, digest)
            logger.info(
                "ExperienceAppraiser: post-verdict update done, trajectory=%s "
                "outcome=%s->%s entries=%d reattributed=%s",
                trajectory_id,
                old_outcome,
                verdict.outcome,
                len(verdict.derived_ids),
                old_outcome != verdict.outcome,
            )
        except Exception:
            logger.warning(
                "ExperienceAppraiser: post-verdict update failed, trajectory=%s",
                trajectory_id,
                exc_info=True,
            )

    # -- 第 4 步：判定成败 ---------------------------------------------------- #

    def _judge_outcome(
        self, record: ExecutionTrajectoryRecord, feedback: _Feedback
    ) -> VerdictRecord:
        """按证据等级表自上而下取第一个有依据的等级（§3.1）。"""
        # external：与该接入方配置的阈值比较，不调模型（阈值键是生产者主体，
        # 取自认证结果，请求无法冒用其他接入方的阈值，§3.1）。
        if record.external_score is not None:
            threshold = self._external_thresholds.get(
                record.producer_principal, DEFAULT_EXTERNAL_THRESHOLD
            )
            score = float(record.external_score)
            if score >= threshold:
                outcome = OUTCOME_SUCCESS
            elif score > 0:
                outcome = OUTCOME_PARTIAL
            else:
                outcome = OUTCOME_FAILURE
            return VerdictRecord(
                outcome=outcome,
                evidence=EVIDENCE_EXTERNAL,
                revision=0,
                judged_by=JUDGED_BY_EXTERNAL,
                judged_at=now_iso(),
            )
        # user_feedback：轨迹内结果之后的用户消息（第 3 步判出的 internal 结论）。
        if feedback.internal in {"correction", "approval"}:
            return VerdictRecord(
                outcome=(
                    OUTCOME_FAILURE if feedback.internal == "correction"
                    else OUTCOME_SUCCESS
                ),
                evidence=EVIDENCE_USER_FEEDBACK,
                revision=0,
                judged_by=JUDGED_BY_MODEL,
                evidence_msgs=(
                    [feedback.internal_msg_index]
                    if feedback.internal_msg_index >= 0 else []
                ),
                judged_at=now_iso(),
            )
        # tool_error：规则，不调模型；有报错且之后同一调用未再成功才构成依据。
        unresolved = unresolved_tool_errors(record.messages)
        if unresolved:
            return VerdictRecord(
                outcome=OUTCOME_FAILURE,
                evidence=EVIDENCE_TOOL_ERROR,
                revision=0,
                judged_by=JUDGED_BY_RULE,
                evidence_msgs=unresolved,
                judged_at=now_iso(),
            )
        # model_only：一次模型调用。
        prompt = _prompt(self._prompts, "outcome", _OUTCOME_PROMPT)
        trace = compress_trace(record.messages)
        payload = self._call_llm(
            prompt,
            f"## Opening request\n{first_user_message(record.messages) or '(none)'}\n\n"
            f"## Execution trace\n{trace}\n\n"
            f"## Final reply\n{last_assistant_reply(record.messages) or '(none)'}",
        )
        outcome = str(payload.get("outcome", OUTCOME_PARTIAL)).strip().lower()
        if outcome not in {OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_FAILURE}:
            outcome = OUTCOME_PARTIAL
        return VerdictRecord(
            outcome=outcome,
            evidence=EVIDENCE_MODEL_ONLY,
            revision=0,
            judged_by=JUDGED_BY_MODEL,
            judged_at=now_iso(),
        )

    # -- 第 5 步：归因 -------------------------------------------------------- #

    def _attribute(
        self,
        scope: Scope,
        record: ExecutionTrajectoryRecord,
        verdict: VerdictRecord,
        digest: str,
    ) -> dict[str, str]:
        """逐条判定取用过的条目与轨迹中加载的技能并写归因事件（§3.2、§2.5.7）。

        接入方不提供使用记录（``retrieved is None``）时经验条目不判定，技能
        照常识别（技能不经检索返回，反馈只能从轨迹里来）。已删除、读不到的
        条目跳过（无法判定其内容）。失败且与已取用条目均无关时在判定记录上
        记覆盖缺口（``coverage_gap``）。技能的归因事件写在配对的技能空间、
        以 ``skill:{技能名}:{版本号}`` 作键（§2.5.5：条目标识改变时使用
        反馈不中断）。
        """
        exp_scope = self._experience_scope_of(scope)
        entries: dict[str, str] = {}
        for item in record.retrieved or []:
            entry_id = str(item.get("id", "")).strip()
            if not entry_id or entry_id in entries:
                continue
            try:
                unit = load_unit(self._kv.get(exp_scope, memory_key(entry_id)))
            except Exception:
                unit = None
            if unit is None:
                logger.warning(
                    "ExperienceAppraiser: retrieved entry missing, skip attribution, "
                    "entry=%s",
                    entry_id,
                )
                continue
            entries[entry_id] = unit.content
        skill_scope, skills = self._identify_skills(scope, record)
        if not entries and not skills:
            return {}
        prompt = _prompt(self._prompts, "attribute", _ATTRIBUTE_PROMPT)
        blocks = [f"[Entry {entry_id}]\n{content}" for entry_id, content in entries.items()]
        blocks.extend(f"[Skill {key}]\n{content}" for key, content in skills.items())
        listing = "\n".join(blocks)
        payload = self._call_llm(
            prompt,
            f"## Task outcome\n{verdict.outcome}\n\n## Retrieved entries\n{listing}\n\n"
            f"## Execution trace\n{compress_trace(record.messages)}",
        )
        raw_attributions = payload.get("attributions")
        attributions: dict[str, str] = {}
        events: list[tuple[Scope, str, AttributionEvent]] = []
        for entry_id in entries:
            conclusion = _parse_attribution(
                raw_attributions.get(entry_id) if isinstance(raw_attributions, dict) else None
            )
            attributions[entry_id] = conclusion
            ref = _ref_of(record.retrieved, entry_id)
            events.append(
                (
                    exp_scope,
                    entry_id,
                    AttributionEvent(
                        verdict=conclusion,
                        evidence=verdict.evidence,
                        author=digest,
                        retrieval_version=ref[0],
                        explore=ref[1],
                        occurred_at=now_iso(),
                    ),
                )
            )
        for skill_key in skills:
            conclusion = _parse_attribution(
                raw_attributions.get(skill_key) if isinstance(raw_attributions, dict) else None
            )
            attributions[skill_key] = conclusion
            events.append(
                (
                    skill_scope,
                    skill_key,
                    # 技能不经检索返回，没有引用值可带回：版本与探索标记取默认。
                    AttributionEvent(
                        verdict=conclusion,
                        evidence=verdict.evidence,
                        author=digest,
                        occurred_at=now_iso(),
                    ),
                )
            )
        unrelated = bool(payload.get("unrelated_failure", False))
        if unrelated and verdict.outcome == OUTCOME_FAILURE:
            verdict.coverage_gap = True
            self._verdicts.save(scope, digest, record.trajectory_id, verdict)
        for write_scope, entry_id, event in events:
            self._utility.write_attribution_event(
                write_scope, entry_id, digest, record.trajectory_id, verdict.revision, event
            )
        return attributions

    def _identify_skills(
        self, scope: Scope, record: ExecutionTrajectoryRecord
    ) -> tuple[Scope | None, dict[str, str]]:
        """从轨迹的工具调用里识别加载的技能（§2.5.7「识别」）。

        找 ``skill_tool``（工具名可配置）的调用取技能名，在经验落点空间中找
        同名已发布技能（标签 approved / trial 且生效）；同名多版取版本号最大
        的现行版。技能与来源经验在同一空间（v3 §2.5：不另建空间、不需要
        配对），识别与归因事件都落在经验落点空间。返回 ``(技能空间,
        {skill:{名}:{版本}: SKILL.md 正文})``；经验落点空间未开启技能沉淀
        （curated_spaces 中该空间 ``skill`` 不为真）或轨迹没加载过技能时返回
        ``(None, {})``。技能名没有已发布的同名版本（agent 本地技能、被下线
        或驳回的）跳过，不归因。
        """
        skill_scope = self._experience_scope_of(scope)
        if skill_scope.space not in self._experience.skill_space_names:
            return None, {}
        names = [
            primary
            for name, primary in tool_call_digest(record.messages)
            if name == self._skill_tool_name and primary
        ]
        if not names:
            return None, {}
        current: dict[str, MemoryUnit] = {}
        for _key, raw in self._kv.scan(skill_scope, MEMORY_KEY_PREFIX):
            unit = load_unit(raw)
            if unit is None or str(
                unit.system_metadata.get("memory_type", "")
            ) != MEMORY_TYPE_SKILL:
                continue
            if unit.lifecycle != LifecycleState.ACTIVE:
                continue
            if not any(tag in SKILL_PUBLISHED_TAGS for tag in unit.tags):
                continue
            name = str(unit.system_metadata.get("skill_name", "")).strip()
            if not name:
                continue
            held = current.get(name)
            if held is None or _skill_version_of(unit) > _skill_version_of(held):
                current[name] = unit
        found: dict[str, str] = {}
        for name in dict.fromkeys(names):
            unit = current.get(name)
            if unit is not None:
                found[f"skill:{name}:{_skill_version_of(unit)}"] = unit.content
        return skill_scope, found

    # -- 值得提炼 ------------------------------------------------------------- #

    def _worth_extracting(
        self, record: ExecutionTrajectoryRecord, feedback: _Feedback
    ) -> bool:
        """四条之一即值得提炼（§2.2）：调用数达阈值 / 带外部结果 / 有报错 /
        本次新输入是对上一轮的纠正。"""
        if record.tool_call_count >= self._tool_call_threshold:
            return True
        if record.external_score is not None:
            return True
        if has_error_mark(record.messages):
            return True
        return feedback.previous == "correction"

    # -- 第 8 步：finalize ---------------------------------------------------- #

    def finalize(self, units: list[MemoryUnit], appraisal: Appraisal, result: Any) -> None:
        """收尾：写复现事件、更新复现作者与类别计数、轨迹置已完成。"""
        if not isinstance(appraisal.data, ExperienceAppraisal):
            return
        exp_appraisal = appraisal.data
        record = exp_appraisal.record
        scope = units[0].scope if units else Scope()
        author = author_principal(user=record.user.user, agent=record.user.agent)
        digest = author_digest(author, self._author_key)
        if result is None:
            record.status = STATUS_FAILED
            self._trajectories.save(scope, digest, record)
            return
        try:
            self._finalize_steps(scope, digest, exp_appraisal, result)
        except Exception:
            record.status = STATUS_FAILED
            self._trajectories.save(scope, digest, record)
            raise

    def _finalize_steps(
        self, scope: Scope, digest: str, exp: ExperienceAppraisal, result: Any
    ) -> None:
        record = exp.record
        verdict = exp.verdict
        retrieved_ids = [str(item.get("id", "")) for item in (record.retrieved or [])]
        exp_scope = self._experience_scope(scope, result)
        # 复现事件：新增的条目与判为重复时命中的已有条目各写一条（§3.2）。
        entry_ids = list(getattr(result, "created_ids", [])) + list(
            getattr(result, "duplicate_ids", [])
        )
        source_scope = [
            scope.org, scope.space, scope.user, scope.agent, scope.session,
        ]
        for entry_id in entry_ids:
            self._utility.write_reproduction_event(
                exp_scope,
                entry_id,
                digest,
                record.trajectory_id,
                verdict.revision,
                ReproductionEvent(
                    source_scope=source_scope,
                    trajectory_id=record.trajectory_id,
                    author=digest,
                    retrieved_ids=retrieved_ids,
                    outcome=verdict.outcome,
                    evidence=verdict.evidence,
                    occurred_at=now_iso(),
                ),
            )
        # 取代产生的新条目：把被取代条目的复现作者记录并入新条目。
        for unit in list(getattr(result, "created_units", [])):
            if not getattr(unit, "supersedes", ""):
                continue
            old_authors = self._utility.load_repro_authors(
                exp_scope, str(unit.supersedes)
            )
            merged = list(old_authors)
            if digest not in merged:
                merged.append(digest)
            self._utility.save_repro_authors(exp_scope, str(unit.id), merged)
        # 复现作者记录与类别计数：新增与重复命中的条目各计一位作者；每新增一条
        # 类别计数加一。并发沉淀可能少计，每轮周期整理校正（§2.5.2）。
        for entry_id in entry_ids:
            self._utility.add_repro_author(exp_scope, entry_id, digest)
        for unit in list(getattr(result, "created_units", [])):
            category = str(unit.system_metadata.get("category", "")).strip()
            if category:
                self._utility.add_category_count(exp_scope, category, active=1)
        # 把这些条目记入判定记录（判定更新时按新结论为它们补写复现事件），
        # 轨迹记录置为已完成。
        verdict.derived_ids = list(entry_ids)
        self._verdicts.save(scope, digest, record.trajectory_id, verdict)
        record.status = STATUS_DONE
        self._trajectories.save(scope, digest, record)
        logger.info(
            "ExperienceAppraiser: trajectory settled, id=%s outcome=%s entries=%d",
            record.trajectory_id,
            verdict.outcome,
            len(entry_ids),
        )

    def _experience_scope(self, scope: Scope, result: Any) -> Scope:
        """效用事件写入的经验空间 scope。

        条目已落盘时以其所在空间为准（带 coords 的请求经落点判定渲染）；没有
        落盘条目时（全部判为重复或丢弃）取 ``default_space`` 解析的落点——与
        抽取算子给候选的落点一致（§2.3）。
        """
        created = list(getattr(result, "created_units", []))
        if created:
            return Scope(org=scope.org, space=str(created[0].scope.space))
        return self._experience_scope_of(scope)

    def _experience_scope_of(self, scope: Scope) -> Scope:
        """经验条目所在空间的 scope：``default_space`` 解析（§2.3）。

        ``own`` 取写入落点（本次写入的 ``MemoryUnit`` 的 scope 所在空间），
        否则取配置的空间名。取用条目的读取、归因与复现事件、技能识别都在
        这个空间。
        """
        return Scope(org=scope.org, space=self._experience.experience_space_of(scope))

    # -- 公共小件 ------------------------------------------------------------- #

    def _check_deadline(self, step: str) -> None:
        """各步开始前检查处理时限；超过即中止并按出错处理（§2.2）。"""
        if self._deadline_started is None:
            return
        elapsed = time.monotonic() - self._deadline_started
        if elapsed > self._time_limit:
            raise TimeoutError(
                f"experience processing time limit exceeded at step {step!r}: "
                f"{elapsed:.0f}s > {self._time_limit:.0f}s"
            )

    def _call_llm(self, system_prompt: str, user_text: str) -> dict:
        """一次模型调用（含重试），返回解析后的 JSON object。"""
        from jiuwen_memory.common.type_def.chat import ChatMessage

        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_text),
        ]
        last_exc: Exception | None = None
        response = ""
        for attempt in range(self._max_attempts):
            try:
                response = self._llm.chat(messages, temperature=0, max_tokens=2048)
                return _parse_object(response)
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_attempts - 1:
                    wait = self._retry_backoff_ms * (2**attempt) / 1000.0
                    logger.warning(
                        "ExperienceAppraiser: LLM call failed (attempt %d), retry in %.1fs",
                        attempt + 1, wait,
                    )
                    time.sleep(wait)
        logger.warning(
            "ExperienceAppraiser: LLM response unparsable after %d attempts: %s",
            self._max_attempts,
            redact_for_log(response[:400]),
        )
        raise last_exc if last_exc else RuntimeError("LLM call did not execute")


# ====================================================================== #
# 请求解析与消息小件
# ====================================================================== #


@dataclass
class _TrajectoryRequest:
    """轨迹请求的概要字段（system_metadata），正文另解析。

    生产者从接口层写入的两个内核保留键取（§2.1「生产者与归属主体」），不取
    请求自报的字符串；键缺失说明写入未经接口层的经验链路，按参数错误拒绝。
    """

    trajectory_id: str = ""
    previous_trajectory_id: str = ""
    tool_call_count: int = 0
    retrieved_count: int | None = None
    producer_principal: str = ""
    credential_id: str = ""
    external_score: float | None = None


def _parse_request_fields(meta: dict[str, Any]) -> _TrajectoryRequest:
    """校验并解析请求概要字段；不合法抛 :class:`ValidationError`（参数错误）。"""
    trajectory_id = str(meta.get("trajectory_id", "")).strip()
    if not trajectory_id:
        raise ValidationError("experience 写入缺少 trajectory_id")
    tool_call_count = _as_int(meta.get("tool_call_count", 0), "tool_call_count")
    if tool_call_count < 0:
        raise ValidationError("tool_call_count 不得为负")
    producer_principal = str(meta.get(PRODUCER_PRINCIPAL_KEY, "")).strip()
    if not producer_principal:
        raise ValidationError(
            "experience 写入缺少生产者标记（接口层从认证结果写入内核保留键，"
            "请求不得自报接入方标识）"
        )
    external_raw = meta.get("external_score")
    external_score = None
    if external_raw is not None and str(external_raw).strip() != "":
        try:
            external_score = float(external_raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError("external_score 须是 0 到 1 的数值") from exc
        if not 0.0 <= external_score <= 1.0:
            raise ValidationError("external_score 须在 0 到 1 之间")
    retrieved_count = meta.get("retrieved_count")
    if retrieved_count is not None and str(retrieved_count).strip() != "":
        retrieved_count = _as_int(retrieved_count, "retrieved_count")
        if retrieved_count < 0:
            raise ValidationError("retrieved_count 不得为负")
    else:
        retrieved_count = None
    return _TrajectoryRequest(
        trajectory_id=trajectory_id,
        previous_trajectory_id=str(meta.get("previous_trajectory_id", "")).strip(),
        tool_call_count=tool_call_count,
        retrieved_count=retrieved_count,
        producer_principal=producer_principal,
        credential_id=str(meta.get(PRODUCER_CREDENTIAL_KEY, "")).strip(),
        external_score=external_score,
    )


def _as_int(raw: Any, name: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} 须是整数") from exc


def _producer_scope_of(principal: str, org: str) -> Scope:
    """从生产者主体（``user:<id>`` / ``agent:<id>``）与 ``scope.org`` 组出 ``producer``。

    主体形态不合法（无前缀或空名）抛参数错误——它来自接口层的内核保留键，
    形态不合法说明写入路径不是正常的经验链路。
    """
    if principal.startswith("user:"):
        name = principal[len("user:"):]
        if name:
            return Scope(org=org, user=name)
    elif principal.startswith("agent:"):
        name = principal[len("agent:"):]
        if name:
            return Scope(org=org, agent=name)
    raise ValidationError(f"experience 写入的生产者标记形态不合法：{principal!r}")


# 背景信息 context 的预定义字段（§2.1「背景信息」）：字段名与类型固定，mem2.0
# 读取；其余字段（除 extra）都是未定义的，出现即参数错误。
_CONTEXT_SCALAR_FIELDS = ("project", "model", "framework")
_CONTEXT_END_STATES = ("completed", "aborted", "timeout")
# extra 的取值约束与 system_metadata 相同（标量或字符串数组），另有数量边界。
_CONTEXT_EXTRA_MAX_KEYS = 32
_CONTEXT_EXTRA_MAX_KEY_LEN = 64
_CONTEXT_EXTRA_MAX_VALUE_LEN = 256


def _validate_context(context: Any) -> dict:
    """校验并归一背景信息（§2.1「背景信息」）；不合法抛参数错误。

    预定义字段（``turn`` / ``started_at`` / ``ended_at`` / ``end_state`` /
    ``project`` / ``model`` / ``framework`` / ``extra``）之外的字段是未定义的，
    出现即拒绝——接入方拼错字段名静默丢弃，比当场报错难排查得多。
    """
    if context is None:
        return {}
    if not isinstance(context, dict):
        raise ValidationError("context 须是对象")
    for key in context:
        if key not in {
            "turn", "started_at", "ended_at", "end_state", "extra",
            *_CONTEXT_SCALAR_FIELDS,
        }:
            raise ValidationError(f"context 出现未定义的字段：{key}")
    turn = context.get("turn")
    if turn is not None and (isinstance(turn, bool) or not isinstance(turn, int)):
        raise ValidationError("context.turn 须是整数")
    for key in ("started_at", "ended_at"):
        value = context.get(key)
        if value is not None and not isinstance(value, str):
            raise ValidationError(f"context.{key} 须是 ISO 8601 字符串")
    end_state = context.get("end_state")
    if end_state is not None and end_state not in _CONTEXT_END_STATES:
        raise ValidationError(
            f"context.end_state 须是 {_CONTEXT_END_STATES} 之一"
        )
    for key in _CONTEXT_SCALAR_FIELDS:
        value = context.get(key)
        if value is not None and not isinstance(value, str):
            raise ValidationError(f"context.{key} 须是字符串")
    extra = context.get("extra")
    if extra is None:
        return dict(context)
    if not isinstance(extra, dict):
        raise ValidationError("context.extra 须是对象")
    if len(extra) > _CONTEXT_EXTRA_MAX_KEYS:
        raise ValidationError(
            f"context.extra 至多 {_CONTEXT_EXTRA_MAX_KEYS} 个键"
        )
    for key, value in extra.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("context.extra 的键须是非空字符串")
        if len(key) > _CONTEXT_EXTRA_MAX_KEY_LEN:
            raise ValidationError(
                f"context.extra 的键名至多 {_CONTEXT_EXTRA_MAX_KEY_LEN} 个字符"
            )
        if isinstance(value, str):
            if len(value) > _CONTEXT_EXTRA_MAX_VALUE_LEN:
                raise ValidationError(
                    f"context.extra 的字符串值至多 {_CONTEXT_EXTRA_MAX_VALUE_LEN} 个字符"
                )
        elif isinstance(value, list):
            if any(not isinstance(item, str) for item in value):
                raise ValidationError("context.extra 的数组元素须是字符串")
            if any(
                len(item) > _CONTEXT_EXTRA_MAX_VALUE_LEN for item in value
            ):
                raise ValidationError(
                    f"context.extra 的字符串值至多 {_CONTEXT_EXTRA_MAX_VALUE_LEN} 个字符"
                )
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("context.extra 的值须是标量或字符串数组")
    return dict(context)


def _parse_trajectory_payload(content: str) -> dict[str, Any]:
    """解析正文为轨迹 JSON 对象；不是合法对象抛参数错误。"""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError("experience 写入的 content 须是轨迹 JSON 字符串") from exc
    if not isinstance(payload, dict):
        raise ValidationError("轨迹 JSON 须是对象")
    return payload


def _validate_messages(messages: list[dict]) -> None:
    if not isinstance(messages, list) or not messages:
        raise ValidationError("轨迹 JSON 缺少 messages 数组")
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not str(message.get("role", "")).strip():
            raise ValidationError(f"messages[{index}] 须是带 role 的对象")


def _normalize_retrieved(
    raw: Any, request: _TrajectoryRequest
) -> list[dict] | None:
    """使用记录归一为 ``[{id, ref}]``；未提供（键缺省或 null）返回 ``None``。

    ``retrieved_count`` 与实际条数不符时按参数错误处理。
    """
    if raw is None:
        if request.retrieved_count not in (None, 0):
            raise ValidationError("retrieved_count 与使用记录不符：未提供使用记录")
        return None
    if not isinstance(raw, list):
        raise ValidationError("retrieved 须是数组")
    items: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not str(item.get("id", "")).strip():
            raise ValidationError(f"retrieved[{index}] 须是带 id 的对象")
        items.append(
            {
                "id": str(item.get("id", "")).strip(),
                "ref": str(item.get("ref", "")).strip(),
            }
        )
    if request.retrieved_count is not None and request.retrieved_count != len(items):
        raise ValidationError(
            f"retrieved_count（{request.retrieved_count}）与使用记录条数（{len(items)}）不符"
        )
    return items


def _ref_of(retrieved: list[dict], entry_id: str) -> tuple[str, bool]:
    """取使用记录中该条目的引用值并解出 ``(取策略版本号, 是否探索名额)``。

    引用值形如 ``0.n`` / ``0.e``，由检索侧的选取算子写入（``common/experience.py``
    的 :func:`parse_usage_ref` 解码）；使用记录无该条目或解析失败按版本 0、
    非探索处理。首版只解出记在归因事件上，策略层不消费。
    """
    for item in retrieved:
        if str(item.get("id", "")) == entry_id:
            return parse_usage_ref(str(item.get("ref", "")))
    return parse_usage_ref("")


def _parse_stance(raw: Any) -> str:
    stance = str(raw or "none").strip().lower()
    return stance if stance in {"correction", "approval", "none"} else "none"


def _parse_attribution(raw: Any) -> str:
    conclusion = str(raw or ATTR_NEUTRAL).strip().lower()
    if conclusion in {ATTR_HELPFUL, ATTR_HARMFUL, ATTR_NEUTRAL}:
        return conclusion
    return ATTR_NEUTRAL


def _skill_version_of(unit: MemoryUnit) -> int:
    """技能条目的版本号（非法值按 1——保守取首版，不因元数据脏而炸沉淀）。"""
    try:
        return int(unit.system_metadata.get("skill_version", 1))
    except (TypeError, ValueError):
        return 1


def _parse_object(response: str) -> dict:
    """解析模型返回的 JSON object；失败再尝试提取最外层花括号段。"""
    text = (response or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise
        payload = json.loads(match.group())
    if not isinstance(payload, dict):
        raise ValueError("appraise 响应必须是 JSON object")
    return payload


# ====================================================================== #
# 装配
# ====================================================================== #


@AppraiserProducer.register("experience")
def _build(config):
    """装配 ExperienceAppraiser；缺 ``experience_author_key`` 时装配失败。

    ``author_key`` 经部署配置的 ``${VAR}`` 从环境变量注入；未配置（展开为空）时
    由构造函数拒绝——作者摘要的 HMAC 密钥缺失就不能落任何轨迹记录（§2.4）。
    经验配置读 ``globals.experience``（v3 §2）：未配置时经验 profile 装配失败
    （经验沉淀以配置了这一项为前提）；落点解析与技能沉淀开关都从这一份取。
    ``skill_tool_name`` 是加载技能的工具名（agent-core 缺省 ``skill_tool``，
    §2.5.7），接入方不同名时在部署配置覆盖。
    """
    from collections.abc import Mapping

    from jiuwen_memory.config.config_source import ConfigSourceProducer
    from jiuwen_memory.construction.experience import parse_experience_config

    experience = parse_experience_config(config.get("experience"))
    if experience is None:
        raise ValidationError(
            "globals.experience 未配置：经验 profile 装配失败（经验沉淀以配置了"
            "这一项为前提，v3 §2）"
        )
    prompts_data = config.get("prompts")
    config_source = ConfigSourceProducer.get_cached("default")
    registry = (
        PromptRegistry.from_dict(prompts_data, config_source=config_source)
        if prompts_data
        else PromptRegistry(config_source=config_source)
    )
    raw_thresholds = config.get("external_thresholds")
    external_thresholds: dict[str, float] = {}
    if isinstance(raw_thresholds, Mapping):
        for producer, threshold in raw_thresholds.items():
            external_thresholds[str(producer)] = float(threshold)
    return ExperienceAppraiser(
        llm=LlmProducer.dep(config, default="echo"),
        kv=resolve_message_store(config),
        author_key=str(config.get("author_key", "")).strip(),
        experience=experience,
        prompt_registry=registry,
        external_thresholds=external_thresholds,
        tool_call_threshold=config.get("tool_call_threshold", DEFAULT_TOOL_CALL_THRESHOLD),
        skill_tool_name=str(config.get("skill_tool_name", SKILL_TOOL_NAME)),
        max_attempts=config.get("appraiser_max_attempts", 3),
        retry_backoff_ms=config.get("appraiser_retry_backoff", 1000),
    )
