"""ExperienceAppraiser：轨迹沉淀八步中的第 1-5 步与第 8 步（mem2.0 §2.2）。

重点覆盖：重复检查的状态机（新轨迹 / done 补报 / processing 冲突 / 超时重试）、
证据等级表的取级顺序（external > user_feedback > tool_error > model_only，高等级
不被低等级覆盖）、归因事件的落盘、finalize 的收尾与失败路径。LLM 用 ScriptedLLM
按调用序编排——各步的调用次数本身就是被测行为（规则级判定不应调模型）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import (
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.experience import (
    ATTR_HARMFUL,
    EVIDENCE_EXTERNAL,
    EVIDENCE_MODEL_ONLY,
    EVIDENCE_TOOL_ERROR,
    EVIDENCE_USER_FEEDBACK,
    OUTCOME_FAILURE,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    UtilityStore,
    attribution_event_key,
    decode_payload,
    now_iso,
    reproduction_event_key,
)
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Modality, Scope, Segment
from jiuwen_memory.common.type_def.memory import LifecycleState, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps as dump_unit
from jiuwen_memory.construction.appraiser import Appraisal
from jiuwen_memory.construction.appraiser_impl.experience_appraiser import (
    ExperienceAppraiser,
)
from jiuwen_memory.construction.experience import (
    JUDGED_BY_EXTERNAL,
    JUDGED_BY_MODEL,
    JUDGED_BY_RULE,
    STATUS_DONE,
    STATUS_FAILED,
    CuratedSpaceConfig,
    ExperienceConfig,
)
from tests.unit.construction.fixtures import ScriptedLLM

pytestmark = pytest.mark.unit

AUTHOR_KEY = "test-author-key"
EXPERIENCE_SPACE = "shared-experience"
SCOPE = Scope(org="acme", user="u1")
EXP_SCOPE = Scope(org="acme", space=EXPERIENCE_SPACE)


def _request_unit(
    *,
    trajectory_id: str = "t1",
    messages: list[dict] | None = None,
    tool_call_count: int = 0,
    retrieved: list | None = None,
    retrieved_count: int | None = None,
    previous: str = "",
    external: float | None = None,
    producer: str = "agent:agent-core",
    scope: Scope = SCOPE,
    context: dict | None = None,
) -> MemoryUnit:
    """构造一条轨迹写入请求 unit：正文是轨迹 JSON，概要字段在 system_metadata。

    ``producer`` 是接口层从认证结果写入内核保留键的生产者主体（v3 §2.1），
    不再是请求自报的接入方标识；``context`` 是正文里的背景信息块（§2.1）。
    """
    payload: dict = {
        "messages": messages
        if messages is not None
        else [{"role": "user", "content": "run the deploy"}]
    }
    if retrieved is not None:
        payload["retrieved"] = retrieved
    if context is not None:
        payload["context"] = context
    meta: dict = {
        "memory_type": "experience",
        "trajectory_id": trajectory_id,
        "tool_call_count": tool_call_count,
        "_producer_principal": producer,
        "_producer_credential": "cred-1",
    }
    if previous:
        meta["previous_trajectory_id"] = previous
    if external is not None:
        meta["external_score"] = external
    if retrieved_count is not None:
        meta["retrieved_count"] = retrieved_count
    return MemoryUnit(
        id="req-1",
        scope=scope,
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=json.dumps(payload), source=Modality.TEXT)],
        system_metadata=meta,
    )


def _entry_unit(entry_id: str, content: str, scope: Scope = EXP_SCOPE) -> MemoryUnit:
    """经验空间里的一条已有条目（归因时按使用记录点读）。"""
    return MemoryUnit(
        id=entry_id,
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
    )


def _seed_entry(kv, entry_id: str, content: str) -> None:
    kv.insert(EXP_SCOPE, memory_key(entry_id), dump_unit(_entry_unit(entry_id, content)))


@dataclass
class _SettleResult:
    """finalize 的落盘结果替身（EvolveResult 的投影）。"""

    created_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    created_units: list[MemoryUnit] = field(default_factory=list)


def _appraiser(kv, llm, **overrides) -> ExperienceAppraiser:
    params = dict(
        llm=llm,
        kv=kv,
        author_key=AUTHOR_KEY,
        # 缺省落点配为空间名：经验落点 = EXP_SCOPE（own 形态由 extractor 另测）。
        experience=ExperienceConfig(default_space=EXPERIENCE_SPACE),
        retry_backoff_ms=0,
    )
    params.update(overrides)
    return ExperienceAppraiser(**params)


def _inject_broken_utility(appraiser, broken) -> None:
    """白盒注入故障替身（效用存储不可用），经小工具绕行受保护访问。"""
    setattr(appraiser, "_utility", broken)


def _post_verdict_update(appraiser, *args, **kwargs) -> None:
    """白盒直测判定回填的防御分支。"""
    getattr(appraiser, "_post_verdict_update")(*args, **kwargs)


def _skill_enabled_config() -> ExperienceConfig:
    """开启技能沉淀的经验配置：经验落点空间声明 ``skill: true``（同空间）。"""
    return ExperienceConfig(
        default_space=EXPERIENCE_SPACE,
        curated_spaces=(CuratedSpaceConfig(space=EXPERIENCE_SPACE, skill=True),),
    )


# ====================================================================== #
# 装配与参数校验
# ====================================================================== #


class TestConstruction:
    @staticmethod
    def test_missing_author_key_rejected():
        with pytest.raises(ValidationError, match="experience_author_key"):
            ExperienceAppraiser(
                llm=ScriptedLLM(),
                kv=_kv(),
                author_key="",
                experience=ExperienceConfig(default_space="s"),
            )


def _kv():
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    return InMemoryKVStore()


class TestRequestValidation:
    @staticmethod
    def test_empty_units_rejected():
        with pytest.raises(ValidationError, match="缺少内容单元"):
            _appraiser(_kv(), ScriptedLLM()).appraise([])

    @staticmethod
    def test_missing_trajectory_id_rejected():
        unit = _request_unit(trajectory_id="")
        with pytest.raises(ValidationError, match="trajectory_id"):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @staticmethod
    def test_missing_producer_rejected():
        unit = _request_unit(producer="")
        with pytest.raises(ValidationError, match="生产者标记"):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @staticmethod
    def test_external_score_out_of_range_rejected():
        unit = _request_unit(external=1.5)
        with pytest.raises(ValidationError, match="external_score"):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @staticmethod
    def test_scope_without_principal_rejected():
        unit = _request_unit(scope=Scope(org="acme"))
        with pytest.raises(ValidationError, match="作者主体"):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @staticmethod
    def test_invalid_payload_json_rejected():
        unit = _request_unit()
        unit.segments = [Segment(content="not json", source=Modality.TEXT)]
        with pytest.raises(ValidationError, match="轨迹 JSON"):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @staticmethod
    def test_empty_messages_rejected():
        unit = _request_unit(messages=[])
        with pytest.raises(ValidationError, match="messages"):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @staticmethod
    def test_retrieved_count_mismatch_rejected():
        unit = _request_unit(
            tool_call_count=1, retrieved=[{"id": "e1", "ref": "0.n"}], retrieved_count=2
        )
        with pytest.raises(ValidationError, match="retrieved_count"):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @pytest.mark.parametrize(
        ("context", "message"),
        [
            ({"turns": 3}, "未定义的字段"),
            ({"turn": "3"}, "context.turn"),
            ({"turn": True}, "context.turn"),
            ({"started_at": 123}, "context.started_at"),
            ({"end_state": "cancelled"}, "context.end_state"),
            ({"project": 7}, "context.project"),
            ({"extra": ["a"]}, "context.extra 须是对象"),
            ({"extra": {f"k{i}": 1 for i in range(33)}}, "至多 32 个键"),
            ({"extra": {"k" * 65: 1}}, "键名至多 64"),
            ({"extra": {"k": "v" * 257}}, "至多 256 个字符"),
            ({"extra": {"k": ["a", 1]}}, "数组元素须是字符串"),
            ({"extra": {"k": {"nested": 1}}}, "标量或字符串数组"),
        ],
    )
    def test_invalid_context_rejected(self, context, message):
        """背景信息块不合法：当场参数错误，不静默丢弃（拼错字段名最难排查）。"""
        unit = _request_unit(context=context)
        with pytest.raises(ValidationError, match=message):
            _appraiser(_kv(), ScriptedLLM()).appraise([unit])

    @staticmethod
    def test_valid_context_reaches_record():
        """合法 context 原样进轨迹记录（含标量与字符串数组的 extra）。"""
        context = {
            "turn": 3,
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:05:00Z",
            "end_state": "completed",
            "project": "deploy-service",
            "model": "glm",
            "framework": "jiwenswarm",
            "extra": {"os": "linux", "tags": ["deploy", "prod"], "retries": 2},
        }
        appraisal = _appraiser(_kv(), ScriptedLLM()).appraise(
            [_request_unit(tool_call_count=1, external=1.0, context=context)]
        )
        record = appraisal.data.record
        assert record.context == context, "背景信息原样落轨迹记录"
        assert record.producer == Scope(org="acme", agent="agent-core"), (
            "生产者从保留键与 scope.org 组出"
        )
        assert record.credential_id == "cred-1", "凭据指纹从保留键取"


# ====================================================================== #
# 第 1 步：重复检查
# ====================================================================== #


class TestDuplicateCheck:
    @staticmethod
    def test_new_trajectory_proceeds():
        kv = _kv()
        llm = ScriptedLLM()
        unit = _request_unit(tool_call_count=1, external=0.5)
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.proceed is True, "新轨迹应放行进入评价"
        assert llm.calls == [], "external 判定不调模型"

    @staticmethod
    def test_done_trajectory_rejected_without_extracting():
        kv = _kv()
        llm = ScriptedLLM()
        appraiser = _appraiser(kv, llm)
        first = appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])
        assert first.proceed is True, "首次写入应放行"
        appraiser.finalize([_request_unit()], first, _SettleResult())
        again = appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])
        assert again.proceed is False, "已处理完的轨迹不再作为新轨迹处理"

    @staticmethod
    def test_processing_trajectory_conflicts():
        kv = _kv()
        appraiser = _appraiser(kv, ScriptedLLM())
        appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])
        # appraise 正常返回后轨迹仍是 processing（finalize 才置 done）。
        with pytest.raises(ConflictError):
            appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])

    @staticmethod
    def test_processing_expired_allows_retry():
        kv = _kv()
        llm = ScriptedLLM()
        appraiser = _appraiser(kv, llm)
        appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])
        # 把轨迹记录改写成很久之前开始处理（进程中途退出的识别）。
        from jiuwen_memory.construction.experience import (
            TrajectoryStore,
            author_digest,
        )

        digest = author_digest("user:u1", AUTHOR_KEY)
        store = TrajectoryStore(kv)
        record = store.load(SCOPE, digest, "t1")
        assert record is not None, "前置：轨迹记录已写入"
        record.started_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        store.save(SCOPE, digest, record)
        appraisal = appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])
        reloaded = store.load(SCOPE, digest, "t1")
        assert appraisal.proceed is True, "超时的处理中记录允许重试"
        assert reloaded is not None and reloaded.retries == 1, "重试次数递增"

    @staticmethod
    def test_failed_trajectory_retry_increments():
        kv = _kv()
        llm = ScriptedLLM(["bad json", "bad json", "bad json"])
        appraiser = _appraiser(kv, llm)
        with pytest.raises(Exception):
            appraiser.appraise(
                [_request_unit(tool_call_count=1, retrieved=[{"id": "e1", "ref": "0.n"}],
                               retrieved_count=1)]
            )
        from jiuwen_memory.construction.experience import TrajectoryStore, author_digest

        digest = author_digest("user:u1", AUTHOR_KEY)
        store = TrajectoryStore(kv)
        failed = store.load(SCOPE, digest, "t1")
        assert failed is not None and failed.status == STATUS_FAILED, (
            "评价失败后轨迹置为失败状态"
        )
        # 失败轨迹重试：正文重新解析，retries 递增。
        llm2 = ScriptedLLM()
        retry = _appraiser(kv, llm2).appraise(
            [_request_unit(tool_call_count=1, external=1.0)]
        )
        assert retry.proceed is True, "失败轨迹可重试"
        reloaded = store.load(SCOPE, digest, "t1")
        assert reloaded is not None and reloaded.retries == 1, "重试次数递增"


class TestExternalSupplement:
    @staticmethod
    def test_done_trajectory_with_external_score_updates_verdict():
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        appraiser = _appraiser(kv, llm)
        # 初判走 model_only（无 external、无报错）：模型判 success。
        unit = _request_unit(
            messages=_retrieved_messages(), tool_call_count=1
        )
        first = appraiser.appraise([unit])
        assert first.proceed is True, "前置：首判完成"
        from jiuwen_memory.construction.experience import VerdictStore, author_digest

        digest = author_digest("user:u1", AUTHOR_KEY)
        verdicts = VerdictStore(kv)
        verdict = verdicts.load(SCOPE, digest, "t1")
        assert verdict is not None and verdict.evidence == EVIDENCE_MODEL_ONLY, (
            "前置：初判为 model_only"
        )
        appraiser.finalize([_request_unit()], first, _SettleResult())
        # 补报外部结果 0 分（初判 success）→ external 级推翻为 failure。
        again = appraiser.appraise([_request_unit(tool_call_count=1, external=0.0)])
        assert again.proceed is False, "补报不产出条目"
        updated = verdicts.load(SCOPE, digest, "t1")
        assert updated is not None, "判定记录应仍存在"
        assert updated.outcome == OUTCOME_FAILURE, "external 补报推翻初判结论"
        assert updated.evidence == EVIDENCE_EXTERNAL, "证据等级升为 external"
        assert updated.judged_by == JUDGED_BY_EXTERNAL, "判定方式为外部补报"
        assert updated.revision == 1, "修订号加一"

    @staticmethod
    def test_same_outcome_supplement_not_reapplied():
        kv = _kv()
        llm = ScriptedLLM()
        appraiser = _appraiser(kv, llm)
        first = appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])
        appraiser.finalize([_request_unit()], first, _SettleResult())
        appraiser.appraise([_request_unit(tool_call_count=1, external=1.0)])
        from jiuwen_memory.construction.experience import VerdictStore, author_digest

        digest = author_digest("user:u1", AUTHOR_KEY)
        verdict = VerdictStore(kv).load(SCOPE, digest, "t1")
        assert verdict is not None and verdict.revision == 0, (
            "补报同 external 级同结论不重复修订"
        )

    @staticmethod
    def test_producer_threshold_configurable():
        kv = _kv()
        llm = ScriptedLLM()
        appraiser = _appraiser(
            kv, llm, external_thresholds={"agent:agent-core": 0.5}
        )
        unit = _request_unit(tool_call_count=1, external=0.7)
        appraisal = appraiser.appraise([unit])
        data = appraisal.data
        assert appraisal.proceed is True and data.verdict.outcome == OUTCOME_SUCCESS, (
            "该接入方阈值 0.5：0.7 达线判成功（默认阈值 0.999 下不可能是 success）"
        )

    @staticmethod
    def test_supplement_from_other_producer_rejected():
        """补报生产者与轨迹记录的生产者不同主体：权限错误，判定不被改写。

        外部结果是证据等级最高的结论来源，只接受发送这条轨迹的接入方补报
        （v3 §补报核对）；换一个接入方身份补报 0 分不能推翻原判定。
        """
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        appraiser = _appraiser(kv, llm)
        first = appraiser.appraise(
            [_request_unit(messages=_retrieved_messages(), tool_call_count=1)]
        )
        appraiser.finalize([_request_unit()], first, _SettleResult())
        from jiuwen_memory.construction.experience import VerdictStore, author_digest

        digest = author_digest("user:u1", AUTHOR_KEY)

        with pytest.raises(PermissionDeniedError, match="same producer"):
            appraiser.appraise(
                [_request_unit(tool_call_count=1, external=0.0, producer="agent:other")]
            )

        verdict = VerdictStore(kv).load(SCOPE, digest, "t1")
        assert verdict is not None and verdict.outcome == OUTCOME_SUCCESS, (
            "他方补报被拒后判定保持初判结论"
        )

    @staticmethod
    def test_supplement_from_user_producer_rejected():
        """``user:`` 主体同样要过生产者核对（轨迹由 agent 发送，用户主体补报被拒）。"""
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        appraiser = _appraiser(kv, llm)
        first = appraiser.appraise(
            [_request_unit(messages=_retrieved_messages(), tool_call_count=1)]
        )
        appraiser.finalize([_request_unit()], first, _SettleResult())

        with pytest.raises(PermissionDeniedError, match="same producer"):
            appraiser.appraise(
                [_request_unit(tool_call_count=1, external=0.0, producer="user:u1")]
            )


# ====================================================================== #
# 第 2 步：结构过滤
# ====================================================================== #


class TestStructureFilter:
    @staticmethod
    def test_no_signals_marks_done_without_processing():
        kv = _kv()
        llm = ScriptedLLM()
        appraisal = _appraiser(kv, llm).appraise([_request_unit()])
        assert appraisal.proceed is False, "三类信号都没有时不再处理"
        assert llm.calls == [], "结构过滤不调模型"
        from jiuwen_memory.construction.experience import TrajectoryStore, author_digest

        digest = author_digest("user:u1", AUTHOR_KEY)
        record = TrajectoryStore(kv).load(SCOPE, digest, "t1")
        assert record is not None and record.status == STATUS_DONE, (
            "无信号轨迹直接置已完成"
        )


# ====================================================================== #
# 第 3 步：判定反馈
# ====================================================================== #


def _verdict_in_kv(kv, trajectory_id: str, **fields) -> None:
    from jiuwen_memory.construction.experience import VerdictRecord, VerdictStore, author_digest

    digest = author_digest("user:u1", AUTHOR_KEY)
    VerdictStore(kv).save(
        SCOPE, digest, trajectory_id, VerdictRecord(judged_at=now_iso(), **fields)
    )


def _seed_trajectory(
    kv,
    trajectory_id: str,
    messages: list[dict],
    retrieved: list | None = None,
    *,
    user: Scope | None = None,
    context: dict | None = None,
) -> None:
    """预置一条轨迹记录（判定反馈要取上一条轨迹的最终回复）。

    ``user`` 是落盘的归属主体四维（会话核对按它的 session 维比对）；
    ``context`` 是背景信息块（会话核对按它的 turn 比对）。
    """
    from jiuwen_memory.construction.experience import (
        ExecutionTrajectoryRecord,
        TrajectoryStore,
        trajectory_expires_at,
    )

    TrajectoryStore(kv).save(
        SCOPE,
        _digest(),
        ExecutionTrajectoryRecord(
            trajectory_id=trajectory_id,
            user=user if user is not None else SCOPE,
            producer=Scope(org="acme", agent="agent-core"),
            messages=messages,
            retrieved=retrieved,
            context=context or {},
            started_at=now_iso(),
            expires_at=trajectory_expires_at(),
        ),
    )


def _load_verdict(kv, trajectory_id: str):
    from jiuwen_memory.construction.experience import VerdictStore, author_digest

    return VerdictStore(kv).load(
        SCOPE, author_digest("user:u1", AUTHOR_KEY), trajectory_id
    )


class TestFeedbackBackfill:
    @staticmethod
    def _two_turn_messages() -> list[dict]:
        return [
            {"role": "user", "content": "run the deploy"},
            {"role": "assistant", "content": "deployed"},
            {"role": "user", "content": "that was wrong, redo it"},  # 本次新输入
        ]

    def test_correction_backfills_previous_failure(self):
        kv = _kv()
        _seed_trajectory(kv, "t0", [
            {"role": "user", "content": "run the deploy"},
            {"role": "assistant", "content": "deployed"},
        ])
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(), tool_call_count=1, previous="t0"
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.proceed is True, "前置：评价完成"
        previous = _load_verdict(kv, "t0")
        assert previous is not None, "上一条判定记录应存在"
        assert previous.outcome == OUTCOME_FAILURE, "correction 回填为失败"
        assert previous.evidence == EVIDENCE_USER_FEEDBACK, "证据等级升为 user_feedback"
        assert previous.revision == 1, "回填修订号加一"

    def test_approval_backfills_previous_success(self):
        kv = _kv()
        _seed_trajectory(kv, "t0", [
            {"role": "user", "content": "run the deploy"},
            {"role": "assistant", "content": "deployed"},
        ])
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_PARTIAL, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "approval", "internal": "none"}),
            json.dumps({"outcome": "partial"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(), tool_call_count=1, previous="t0"
        )
        _appraiser(kv, llm).appraise([unit])
        previous = _load_verdict(kv, "t0")
        assert previous is not None and previous.outcome == OUTCOME_SUCCESS, (
            "approval 回填为成功"
        )

    def test_none_stance_does_not_backfill(self):
        kv = _kv()
        _seed_trajectory(kv, "t0", [
            {"role": "user", "content": "run the deploy"},
            {"role": "assistant", "content": "deployed"},
        ])
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_PARTIAL, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "none", "internal": "none"}),
            json.dumps({"outcome": "partial"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(), tool_call_count=1, previous="t0"
        )
        _appraiser(kv, llm).appraise([unit])
        previous = _load_verdict(kv, "t0")
        assert previous is not None and previous.outcome == OUTCOME_PARTIAL, (
            "none 结论不回填"
        )

    def test_higher_evidence_not_overwritten(self):
        kv = _kv()
        _seed_trajectory(kv, "t0", [
            {"role": "user", "content": "run the deploy"},
            {"role": "assistant", "content": "deployed"},
        ])
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_EXTERNAL)
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(), tool_call_count=1, previous="t0"
        )
        _appraiser(kv, llm).appraise([unit])
        previous = _load_verdict(kv, "t0")
        assert previous is not None, "前置：记录存在"
        assert previous.evidence == EVIDENCE_EXTERNAL, "external 级不被 user_feedback 覆盖"
        assert previous.outcome == OUTCOME_SUCCESS, "结论保持原判"

    @staticmethod
    def test_no_pairs_no_llm_call():
        """单轮无 previous：两对都不存在，反馈步不调模型。"""
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run"},
                {"role": "assistant", "content": "done"},
            ],
            tool_call_count=1,
        )
        _appraiser(kv, llm).appraise([unit])
        assert len(llm.calls) == 1, "只有成败判定一次调用（model_only 级）"

    @staticmethod
    def test_internal_correction_drives_outcome():
        """轨迹内多轮：最后一段结果之后的用户纠错直接给出成败判定。"""
        kv = _kv()
        llm = ScriptedLLM([
            json.dumps({"previous": "none", "internal": "correction"}),
        ])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
                {"role": "user", "content": "that was wrong"},
                {"role": "assistant", "content": "redone"},
            ],
            tool_call_count=1,
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.proceed is True, "前置：评价完成"
        assert appraisal.data.verdict.outcome == OUTCOME_FAILURE, (
            "internal correction 判为失败"
        )
        assert appraisal.data.verdict.evidence == EVIDENCE_USER_FEEDBACK, (
            "证据等级为 user_feedback"
        )
        assert len(llm.calls) == 1, "user_feedback 级不调成败模型"

    # -- 会话核对（v3 §3.1「判定反馈」）：只 gate 回填，不 gate Pair P ------------ #

    def test_cross_session_feedback_not_backfilled(self):
        """上一条轨迹在别的会话：反馈不回填（判定记录不变），Pair P 照常判定。

        记录可读（save 落点相同）但归属会话不同——跨会话的输入不是对上一条
        结果的反馈。
        """
        kv = _kv()
        _seed_trajectory(
            kv, "t0",
            [
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
            ],
            user=Scope(org="acme", user="u1", session="s1"),
        )
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(), tool_call_count=1, previous="t0"
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        previous = _load_verdict(kv, "t0")
        assert previous is not None and previous.revision == 0, (
            "跨会话反馈不回填，判定记录保持初判"
        )
        assert previous.evidence == EVIDENCE_MODEL_ONLY, "证据等级不变"
        assert appraisal.proceed is True, "本条轨迹照常评价（Pair P 不受核对影响）"
        assert len(llm.calls) == 2, "反馈与成败判定两次调用照常发生"

    def test_turn_not_increasing_skips_backfill(self):
        """本条 turn 不比上一条大：无从证明是紧接着的下一轮，不回填。"""
        kv = _kv()
        _seed_trajectory(
            kv, "t0",
            [
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
            ],
            context={"turn": 5},
        )
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(),
            tool_call_count=1, previous="t0", context={"turn": 5},
        )
        _appraiser(kv, llm).appraise([unit])
        previous = _load_verdict(kv, "t0")
        assert previous is not None and previous.revision == 0, (
            "turn 不递增不回填"
        )

    def test_turn_missing_on_previous_skips_backfill(self):
        """本条带 turn 而上一条没有：轮次无从核对，按不满足处理（保守不回填）。"""
        kv = _kv()
        _seed_trajectory(kv, "t0", [
            {"role": "user", "content": "run the deploy"},
            {"role": "assistant", "content": "deployed"},
        ])
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(),
            tool_call_count=1, previous="t0", context={"turn": 3},
        )
        _appraiser(kv, llm).appraise([unit])
        previous = _load_verdict(kv, "t0")
        assert previous is not None and previous.revision == 0, (
            "上一条无 turn 不回填"
        )

    def test_same_conversation_increasing_turn_backfills(self):
        """同会话且 turn 递增：照常回填（核对通过的正路径）。"""
        kv = _kv()
        _seed_trajectory(
            kv, "t0",
            [
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
            ],
            context={"turn": 2},
        )
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=self._two_turn_messages(),
            tool_call_count=1, previous="t0", context={"turn": 3},
        )
        _appraiser(kv, llm).appraise([unit])
        previous = _load_verdict(kv, "t0")
        assert previous is not None and previous.revision == 1, "核对通过照常回填"
        assert previous.outcome == OUTCOME_FAILURE, "correction 回填为失败"


# ====================================================================== #
# 第 4 步：判定成败
# ====================================================================== #


class TestOutcomeEvidenceLadder:
    @staticmethod
    def test_external_score_high_is_success():
        kv = _kv()
        llm = ScriptedLLM()
        unit = _request_unit(tool_call_count=1, external=1.0)
        appraisal = _appraiser(kv, llm).appraise([unit])
        verdict = appraisal.data.verdict
        assert verdict.outcome == OUTCOME_SUCCESS, "external 满分判成功"
        assert verdict.evidence == EVIDENCE_EXTERNAL, "证据等级 external"
        assert verdict.judged_by == JUDGED_BY_EXTERNAL, "判定方式 external"

    @staticmethod
    def test_external_score_partial():
        kv = _kv()
        llm = ScriptedLLM()
        unit = _request_unit(tool_call_count=1, external=0.5)
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.data.verdict.outcome == OUTCOME_PARTIAL, (
            "external 中间值判部分成功"
        )

    @staticmethod
    def test_external_score_zero_is_failure():
        kv = _kv()
        llm = ScriptedLLM()
        unit = _request_unit(tool_call_count=1, external=0.0)
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.data.verdict.outcome == OUTCOME_FAILURE, "external 零分判失败"

    @staticmethod
    def test_unresolved_tool_error_is_rule_failure():
        kv = _kv()
        llm = ScriptedLLM()
        messages = [
            {"role": "user", "content": "run the app"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command": "python -m app"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "config/app.yaml not found",
                "is_error": True,
            },
            {"role": "assistant", "content": "gave up"},
        ]
        unit = _request_unit(messages=messages, tool_call_count=1)
        appraisal = _appraiser(kv, llm).appraise([unit])
        verdict = appraisal.data.verdict
        assert verdict.outcome == OUTCOME_FAILURE, "未恢复报错判失败"
        assert verdict.evidence == EVIDENCE_TOOL_ERROR, "证据等级 tool_error"
        assert verdict.judged_by == JUDGED_BY_RULE, "规则判定不调模型"
        assert verdict.evidence_msgs == [2], "依据消息下标记入判定"
        assert llm.calls == [], "tool_error 级不调模型"

    @staticmethod
    def test_model_only_falls_through_to_llm():
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run"},
                {"role": "assistant", "content": "done"},
            ],
            tool_call_count=1,
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        verdict = appraisal.data.verdict
        assert verdict.outcome == OUTCOME_SUCCESS, "模型结论采纳"
        assert verdict.evidence == EVIDENCE_MODEL_ONLY, "证据等级 model_only"
        assert verdict.judged_by == JUDGED_BY_MODEL, "判定方式 model"
        assert len(llm.calls) == 1, "恰好一次模型调用"

    @staticmethod
    def test_invalid_llm_outcome_defaults_partial():
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "banana"})])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run"},
                {"role": "assistant", "content": "done"},
            ],
            tool_call_count=1,
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.data.verdict.outcome == OUTCOME_PARTIAL, (
            "模型返回非法结论时归 partial"
        )


# ====================================================================== #
# 第 5 步：归因
# ====================================================================== #


def _retrieved_messages() -> list[dict]:
    return [
        {"role": "user", "content": "run the deploy"},
        {"role": "assistant", "content": "deployed fine"},
    ]


class TestAttribution:
    @staticmethod
    def test_no_retrieved_skips_attribution():
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        unit = _request_unit(messages=_retrieved_messages(), tool_call_count=6)
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.data.attribution == {}, "未提供使用记录不归因"
        assert len(llm.calls) == 1, "只有成败判定一次调用"

    @staticmethod
    def test_no_retrieved_no_skill_still_skips():
        """未配技能空间时轨迹里的 skill_tool 调用不触发归因（未开启）。"""
        kv = _kv()
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        unit = _request_unit(messages=_skill_messages(), tool_call_count=6)
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.data.attribution == {}, "未配技能空间不归因"
        assert len(llm.calls) == 1, "只有成败判定一次调用"

    @staticmethod
    def test_attribution_writes_events():
        kv = _kv()
        _seed_entry(kv, "e1", "tip: use cp to create config")
        _seed_entry(kv, "e2", "fact: app needs config/app.yaml")
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"e1": "helpful", "e2": "neutral"},
                "unrelated_failure": False,
            }),
        ])
        unit = _request_unit(
            messages=_retrieved_messages(),
            tool_call_count=6,
            retrieved=[{"id": "e1", "ref": "0.n"}, {"id": "e2", "ref": "0.e"}],
            retrieved_count=2,
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.data.attribution == {"e1": "helpful", "e2": "neutral"}, (
            "归因结论传给抽取算子"
        )
        assert kv.exists(EXP_SCOPE, attribution_event_key("e1", _digest(), "t1", 0)), (
            "e1 的归因事件已写入经验空间"
        )
        assert kv.exists(EXP_SCOPE, attribution_event_key("e2", _digest(), "t1", 0)), (
            "e2 的归因事件已写入经验空间"
        )

    @staticmethod
    def test_missing_entry_skipped():
        kv = _kv()
        _seed_entry(kv, "e1", "tip: use cp to create config")
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"e1": "helpful"}, "unrelated_failure": False
            }),
        ])
        unit = _request_unit(
            messages=_retrieved_messages(),
            tool_call_count=6,
            retrieved=[{"id": "e1", "ref": "0.n"}, {"id": "gone", "ref": "0.n"}],
            retrieved_count=2,
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.data.attribution == {"e1": "helpful"}, (
            "读不到的条目跳过，不进归因"
        )

    @staticmethod
    def test_unrelated_failure_marks_coverage_gap():
        kv = _kv()
        _seed_entry(kv, "e1", "tip: use cp to create config")
        llm = ScriptedLLM([
            json.dumps({"outcome": "failure"}),
            json.dumps({
                "attributions": {"e1": "neutral"}, "unrelated_failure": True
            }),
        ])
        unit = _request_unit(
            messages=_retrieved_messages(),
            tool_call_count=6,
            retrieved=[{"id": "e1", "ref": "0.n"}],
            retrieved_count=1,
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        verdict = _load_verdict(kv, "t1")
        assert appraisal.data.verdict.coverage_gap is True, "覆盖缺口传给抽取算子"
        assert verdict is not None and verdict.coverage_gap is True, (
            "覆盖缺口已写入判定记录"
        )


# ====================================================================== #
# 第 5 步：技能的识别与归因（§2.5.7；v3 §2.5 技能与经验同空间）
# ======================================================================


def _skill_messages(skill_name: str = "deploy-config") -> list[dict]:
    """轨迹消息：含一次 skill_tool 加载调用（agent-core 的技能加载形态）。"""
    return [
        {"role": "user", "content": "run the deploy"},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "c1",
                "function": {
                    "name": "skill_tool",
                    "arguments": json.dumps({"skill_name": skill_name}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "loaded"},
        {"role": "assistant", "content": "deployed fine"},
    ]


def _seed_skill(
    kv,
    name: str,
    *,
    version: int = 1,
    tags: tuple[str, ...] = ("approved",),
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
) -> None:
    """经验落点空间里的一条技能条目（v3 §2.5 技能与经验同空间）。"""
    unit = MemoryUnit(
        id=f"skill-{name}-{version}",
        scope=EXP_SCOPE,
        tier=MemoryTier.PROCEDURAL,
        lifecycle=lifecycle,
        segments=[Segment(content=f"# {name}\n1. 复制示例配置", source=Modality.TEXT)],
        tags=list(tags),
        system_metadata={
            "memory_type": "skill",
            "skill_name": name,
            "skill_version": version,
            "category": "deploy",
        },
    )
    kv.insert(EXP_SCOPE, memory_key(unit.id), dump_unit(unit))


class TestSkillAttribution:
    @staticmethod
    def test_skill_identified_and_attributed():
        """轨迹加载过技能时识别并归因：事件写经验落点空间、键 skill:{名}:{版本}。"""
        kv = _kv()
        _seed_skill(kv, "deploy-config")
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"skill:deploy-config:1": "helpful"},
                "unrelated_failure": False,
            }),
        ])
        unit = _request_unit(messages=_skill_messages(), tool_call_count=6)
        appraisal = _appraiser(kv, llm, experience=_skill_enabled_config()).appraise([unit])

        assert appraisal.data.attribution == {"skill:deploy-config:1": "helpful"}, (
            "技能按 skill:{名}:{版本} 键归因"
        )
        assert kv.exists(
            EXP_SCOPE, attribution_event_key("skill:deploy-config:1", _digest(), "t1", 0)
        ), "技能事件写经验落点空间（技能与经验同空间）"
        assert len(llm.calls) == 2, "成败判定 + 归因各一次调用"

    @staticmethod
    def test_skill_and_entries_attributed_together():
        """技能与取用过的经验同一次模型调用归因（§2.5.7「与经验一起归因」）。"""
        kv = _kv()
        _seed_entry(kv, "e1", "tip: use cp to create config")
        _seed_skill(kv, "deploy-config")
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"e1": "helpful", "skill:deploy-config:1": "harmful"},
                "unrelated_failure": False,
            }),
        ])
        unit = _request_unit(
            messages=_skill_messages(),
            tool_call_count=6,
            retrieved=[{"id": "e1", "ref": "0.n"}],
            retrieved_count=1,
        )
        appraisal = _appraiser(kv, llm, experience=_skill_enabled_config()).appraise([unit])

        assert appraisal.data.attribution == {
            "e1": "helpful", "skill:deploy-config:1": "harmful",
        }, "经验与技能的结论都在"
        assert kv.exists(EXP_SCOPE, attribution_event_key("e1", _digest(), "t1", 0)), (
            "经验事件写经验落点空间"
        )
        assert kv.exists(
            EXP_SCOPE, attribution_event_key("skill:deploy-config:1", _digest(), "t1", 0)
        ), "技能事件与经验事件同写经验落点空间"
        assert len(llm.calls) == 2, "两者合在同一次归因调用里"

    @staticmethod
    def test_unpublished_skill_skipped():
        """同名技能未发布（draft）时跳过：不进归因、不调模型。"""
        kv = _kv()
        _seed_skill(kv, "deploy-config", tags=("draft",))
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        unit = _request_unit(messages=_skill_messages(), tool_call_count=6)
        appraisal = _appraiser(kv, llm, experience=_skill_enabled_config()).appraise([unit])

        assert appraisal.data.attribution == {}, "draft 技能不归因"
        assert len(llm.calls) == 1, "只有成败判定一次调用"

    @staticmethod
    def test_trial_skill_counts_as_published():
        """试用（trial）也算已发布：照常归因（§2.5.4 已发布 = 通过或试用）。"""
        kv = _kv()
        _seed_skill(kv, "deploy-config", tags=("trial",))
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"skill:deploy-config:1": "neutral"},
                "unrelated_failure": False,
            }),
        ])
        unit = _request_unit(messages=_skill_messages(), tool_call_count=6)
        appraisal = _appraiser(kv, llm, experience=_skill_enabled_config()).appraise([unit])

        assert appraisal.data.attribution == {"skill:deploy-config:1": "neutral"}

    @staticmethod
    def test_latest_version_attributed():
        """同名多版时归因现行版（版本号最大的已发布版）。"""
        kv = _kv()
        _seed_skill(kv, "deploy-config", version=1, tags=("approved",))
        _seed_skill(kv, "deploy-config", version=2, tags=("trial",))
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"skill:deploy-config:2": "helpful"},
                "unrelated_failure": False,
            }),
        ])
        unit = _request_unit(messages=_skill_messages(), tool_call_count=6)
        appraisal = _appraiser(kv, llm, experience=_skill_enabled_config()).appraise([unit])

        assert appraisal.data.attribution == {"skill:deploy-config:2": "helpful"}, (
            "归因落在版本号最大的现行版"
        )

    @staticmethod
    def test_unknown_skill_name_skipped():
        """技能名在技能空间没有已发布的同名版本（本地技能）：跳过。"""
        kv = _kv()
        _seed_skill(kv, "deploy-config")
        llm = ScriptedLLM([json.dumps({"outcome": "success"})])
        unit = _request_unit(messages=_skill_messages("local-only"), tool_call_count=6)
        appraisal = _appraiser(kv, llm, experience=_skill_enabled_config()).appraise([unit])

        assert appraisal.data.attribution == {}, "无同名发布版不归因"
        assert len(llm.calls) == 1

    @staticmethod
    def test_custom_skill_tool_name():
        """加载工具名可配置：skill_tool_name 覆盖后按新名识别。"""
        kv = _kv()
        _seed_skill(kv, "deploy-config")
        messages = [
            {"role": "user", "content": "run the deploy"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "c1",
                    "function": {
                        "name": "load_skill",
                        "arguments": json.dumps({"skill_name": "deploy-config"}),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "loaded"},
            {"role": "assistant", "content": "deployed fine"},
        ]
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"skill:deploy-config:1": "helpful"},
                "unrelated_failure": False,
            }),
        ])
        unit = _request_unit(messages=messages, tool_call_count=6)
        appraisal = _appraiser(
            kv, llm, experience=_skill_enabled_config(), skill_tool_name="load_skill"
        ).appraise([unit])

        assert appraisal.data.attribution == {"skill:deploy-config:1": "helpful"}


def _digest() -> str:
    from jiuwen_memory.construction.experience import author_digest

    return author_digest("user:u1", AUTHOR_KEY)


# ====================================================================== #
# 值得提炼
# ====================================================================== #


class TestWorthExtracting:
    @staticmethod
    def _appraise(kv, llm, **kwargs) -> Appraisal:
        return _appraiser(kv, llm).appraise([_request_unit(**kwargs)])

    def test_tool_call_count_reaches_threshold(self):
        appraisal = self._appraise(
            _kv(), ScriptedLLM([json.dumps({"outcome": "success"})]),
            messages=_retrieved_messages(), tool_call_count=5,
        )
        assert appraisal.data.worth_extracting is True, "调用数达阈值值得提炼"

    def test_external_score_present(self):
        appraisal = self._appraise(
            _kv(), ScriptedLLM(), messages=_retrieved_messages(),
            tool_call_count=1, external=0.5,
        )
        assert appraisal.data.worth_extracting is True, "带外部结果值得提炼"

    def test_error_mark_even_recovered(self):
        messages = [
            {"role": "user", "content": "run the app"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command": "python -m app"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "config missing",
                "is_error": True,
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c2",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command": "cp a b && python -m app"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c2", "content": "started"},
        ]
        appraisal = self._appraise(
            _kv(), ScriptedLLM([json.dumps({"outcome": "success"})]),
            messages=messages, tool_call_count=2,
        )
        assert appraisal.data.worth_extracting is True, "已恢复的报错同样值得提炼"

    @staticmethod
    def test_previous_correction():
        kv = _kv()
        _seed_trajectory(kv, "t0", [
            {"role": "user", "content": "run"},
            {"role": "assistant", "content": "done"},
        ])
        _verdict_in_kv(kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY)
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({"outcome": "partial"}),
        ])
        appraisal = _appraiser(kv, llm).appraise([
            _request_unit(
                messages=[
                    {"role": "user", "content": "run"},
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "wrong, redo"},
                ],
                tool_call_count=1, previous="t0",
            )
        ])
        assert appraisal.data.worth_extracting is True, (
            "本次新输入是对上一轮的纠正值得提炼"
        )

    def test_none_of_four_not_worth(self):
        appraisal = self._appraise(
            _kv(), ScriptedLLM([json.dumps({"outcome": "success"})]),
            messages=_retrieved_messages(), tool_call_count=1,
        )
        assert appraisal.data.worth_extracting is False, "四条都不满足不值得提炼"


# ====================================================================== #
# 重试、超时与失败状态
# ====================================================================== #


class TestRetryAndTimeout:
    @staticmethod
    def test_llm_retry_then_success():
        kv = _kv()
        llm = ScriptedLLM([
            ValueError("boom"), json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run"},
                {"role": "assistant", "content": "done"},
            ],
            tool_call_count=1,
        )
        appraisal = _appraiser(kv, llm).appraise([unit])
        assert appraisal.proceed is True, "重试后成功应放行"
        assert len(llm.calls) == 2, "失败一次重试一次"

    @staticmethod
    def test_llm_exhausted_marks_failed():
        kv = _kv()
        llm = ScriptedLLM([
            ValueError("boom"), ValueError("boom"), ValueError("boom"),
        ])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run"},
                {"role": "assistant", "content": "done"},
            ],
            tool_call_count=1,
        )
        with pytest.raises(ValueError):
            _appraiser(kv, llm).appraise([unit])
        record = _load_trajectory(kv, "t1")
        assert record is not None and record.status == STATUS_FAILED, (
            "模型调用耗尽后轨迹置失败"
        )

    @staticmethod
    def test_time_limit_exceeded_marks_failed():
        kv = _kv()
        llm = ScriptedLLM()
        unit = _request_unit(tool_call_count=1)
        # 限值取负数等效「已超时」：Windows 时钟分辨率约 15ms，取 0 时两次
        # monotonic 读数相同（elapsed == 0.0 不大于 0.0），无法稳定触发。
        with pytest.raises(TimeoutError):
            _appraiser(kv, llm, processing_time_limit=-1.0).appraise([unit])
        record = _load_trajectory(kv, "t1")
        assert record is not None and record.status == STATUS_FAILED, (
            "处理超时后轨迹置失败"
        )


def _load_trajectory(kv, trajectory_id: str):
    from jiuwen_memory.construction.experience import TrajectoryStore

    return TrajectoryStore(kv).load(SCOPE, _digest(), trajectory_id)


# ====================================================================== #
# 第 8 步：finalize
# ====================================================================== #


def _settle(kv, llm=None, **request_kwargs) -> tuple[ExperienceAppraiser, Appraisal]:
    llm = llm or ScriptedLLM([json.dumps({"outcome": "success"})])
    appraiser = _appraiser(kv, llm)
    appraisal = appraiser.appraise([_request_unit(**request_kwargs)])
    assert appraisal.proceed is True, "前置：评价放行"
    return appraiser, appraisal


class TestFinalize:
    @staticmethod
    def test_none_result_marks_failed():
        kv = _kv()
        appraiser, appraisal = _settle(
            kv, messages=_retrieved_messages(), tool_call_count=1
        )
        appraiser.finalize([_request_unit()], appraisal, None)
        record = _load_trajectory(kv, "t1")
        assert record is not None and record.status == STATUS_FAILED, (
            "流程异常（result=None）时轨迹置失败"
        )

    @staticmethod
    def test_finalize_writes_reproduction_events_and_counts():
        kv = _kv()
        appraiser, appraisal = _settle(
            kv, messages=_retrieved_messages(), tool_call_count=1
        )
        created = _entry_unit("e9", "fact: app needs config", scope=EXP_SCOPE)
        created.system_metadata = {"category": "software-engineering-devops"}
        result = _SettleResult(
            created_ids=["e9"], duplicate_ids=["e8"], created_units=[created]
        )
        appraiser.finalize([_request_unit()], appraisal, result)
        record = _load_trajectory(kv, "t1")
        assert record is not None and record.status == STATUS_DONE, "收尾后轨迹置已完成"
        verdict = _load_verdict(kv, "t1")
        assert verdict is not None and verdict.derived_ids == ["e9", "e8"], (
            "新增与重复命中的条目记入判定记录"
        )
        utility = UtilityStore(kv)
        assert utility.load_repro_authors(EXP_SCOPE, "e9") == [_digest()], (
            "新增条目记复现作者"
        )
        assert utility.load_repro_authors(EXP_SCOPE, "e8") == [_digest()], (
            "重复命中的条目同样记复现作者"
        )
        counts = utility.load_category_count(EXP_SCOPE, "software-engineering-devops")
        assert counts["active"] == 1, "每新增一条类别计数加一"

    @staticmethod
    def test_supersede_merges_repro_authors():
        kv = _kv()
        appraiser, appraisal = _settle(
            kv, messages=_retrieved_messages(), tool_call_count=1
        )
        UtilityStore(kv).save_repro_authors(EXP_SCOPE, "old-1", ["author-x"])
        new_unit = _entry_unit("new-1", "tip: revised", scope=EXP_SCOPE)
        new_unit.supersedes = "old-1"
        result = _SettleResult(created_ids=["new-1"], created_units=[new_unit])
        appraiser.finalize([_request_unit()], appraisal, result)
        authors = UtilityStore(kv).load_repro_authors(EXP_SCOPE, "new-1")
        assert "author-x" in authors, "被取代条目的复现作者并入新条目"
        assert _digest() in authors, "本次作者同样记入新条目"

    @staticmethod
    def test_finalize_failure_marks_failed_and_reraises():
        kv = _kv()
        appraiser, appraisal = _settle(
            kv, messages=_retrieved_messages(), tool_call_count=1
        )
        # 收尾自身出错：utility 写复现事件抛错。
        broken = SimpleNamespace(
            write_reproduction_event=lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("kv down")
            )
        )
        _inject_broken_utility(appraiser, broken)
        result = _SettleResult(created_ids=["e9"])
        with pytest.raises(RuntimeError, match="kv down"):
            appraiser.finalize([_request_unit()], appraisal, result)
        record = _load_trajectory(kv, "t1")
        assert record is not None and record.status == STATUS_FAILED, (
            "收尾出错时轨迹置失败再上抛"
        )

    @staticmethod
    def test_non_experience_appraisal_ignored():
        kv = _kv()
        appraiser = _appraiser(kv, ScriptedLLM())
        appraiser.finalize([], Appraisal(proceed=True, data=None), _SettleResult())
        record = _load_trajectory(kv, "t1")
        assert record is None, "非经验评价结论的 finalize 不写任何记录"


# ====================================================================== #
# 判定更新后的补做（§3.1 判定更新链）
# ====================================================================== #


def _repro_payload(kv, entry_id: str, trajectory_id: str, revision: int) -> dict:
    return decode_payload(
        kv.get(EXP_SCOPE, reproduction_event_key(entry_id, _digest(), trajectory_id, revision))
    )


def _attr_payload(kv, entry_id: str, trajectory_id: str, revision: int) -> dict:
    return decode_payload(
        kv.get(EXP_SCOPE, attribution_event_key(entry_id, _digest(), trajectory_id, revision))
    )


class TestVerdictUpdateChain:
    @staticmethod
    def test_supplement_flip_rewrites_reproduction_events():
        """补报翻转结论：derived_ids 按新结论补写复现事件（新修订号键）。"""
        kv = _kv()
        appraiser, appraisal = _settle(
            kv, messages=_retrieved_messages(), tool_call_count=1
        )
        result = _SettleResult(created_ids=["e9"])
        appraiser.finalize([_request_unit()], appraisal, result)
        assert kv.exists(
            EXP_SCOPE, reproduction_event_key("e9", _digest(), "t1", 0)
        ), "前置：收尾已写 revision 0 的复现事件"
        # 补报 0 分（初判 model_only success）→ external 级推翻为 failure。
        appraiser.appraise([_request_unit(tool_call_count=1, external=0.0)])
        payload = _repro_payload(kv, "e9", "t1", 1)
        assert payload["outcome"] == OUTCOME_FAILURE, "补写事件取新结论"
        assert payload["evidence"] == EVIDENCE_EXTERNAL, "补写事件取新证据等级"
        assert payload["retrieved_ids"] == [], "使用记录取轨迹记录（本次未提供）"
        # 同级同结论的重复补报不更新判定、不重复补写。
        appraiser.appraise([_request_unit(tool_call_count=1, external=0.0)])
        events = [
            key for key, _raw in UtilityStore(kv).scan_events(EXP_SCOPE)
            if key.entry_id == "e9" and key.kind == "repro"
        ]
        assert len(events) == 2, "重复补报不追加事件（revision 0 与 1 各一条）"

    @staticmethod
    def test_supplement_flip_reattributes_retrieved_entries():
        """补报翻转结论：对取用过的条目重新归因，归因事件带新修订号。"""
        kv = _kv()
        _seed_entry(kv, "e1", "tip: use cp to create config")
        llm = ScriptedLLM([
            json.dumps({"outcome": "success"}),
            json.dumps({
                "attributions": {"e1": "helpful"}, "unrelated_failure": False
            }),
            json.dumps({
                "attributions": {"e1": "harmful"}, "unrelated_failure": False
            }),
        ])
        appraiser, appraisal = _settle(
            kv, llm=llm, messages=_retrieved_messages(), tool_call_count=1,
            retrieved=[{"id": "e1", "ref": "0.n"}], retrieved_count=1,
        )
        appraiser.finalize([_request_unit()], appraisal, _SettleResult())
        assert kv.exists(
            EXP_SCOPE, attribution_event_key("e1", _digest(), "t1", 0)
        ), "前置：初次归因已写 revision 0 事件"
        appraiser.appraise([_request_unit(tool_call_count=1, external=0.0)])
        payload = _attr_payload(kv, "e1", "t1", 1)
        assert payload["verdict"] == ATTR_HARMFUL, "重新归因写入新结论"
        assert payload["evidence"] == EVIDENCE_EXTERNAL, "归因事件带新证据等级"
        assert not kv.exists(
            EXP_SCOPE, reproduction_event_key("e1", _digest(), "t1", 1)
        ), "derived_ids 为空（未产出条目）不补写复现事件"

    @staticmethod
    def test_feedback_evidence_upgrade_without_flip_skips_reattribution():
        """回填升级证据但结论未翻转：补写复现事件、不重新归因。"""
        kv = _kv()
        _seed_trajectory(
            kv, "t0", [
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
            ],
            retrieved=[{"id": "e1", "ref": "0.n"}],
        )
        _verdict_in_kv(
            kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY,
            derived_ids=["e9"],
        )
        llm = ScriptedLLM([
            json.dumps({"previous": "approval", "internal": "none"}),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
                {"role": "user", "content": "great, thanks"},
            ],
            tool_call_count=1, previous="t0",
        )
        _appraiser(kv, llm).appraise([unit])
        payload = _repro_payload(kv, "e9", "t0", 1)
        assert payload["outcome"] == OUTCOME_SUCCESS, "结论未翻转取原结论"
        assert payload["evidence"] == EVIDENCE_USER_FEEDBACK, (
            "证据等级升级写进补写事件（升为技能只认这两级）"
        )
        assert payload["retrieved_ids"] == ["e1"], "使用记录取上一条轨迹的记录"
        assert not kv.exists(
            EXP_SCOPE, attribution_event_key("e1", _digest(), "t0", 1)
        ), "结论未翻转不重新归因"
        assert len(llm.calls) == 2, "只有反馈与判定两次调用，无重新归因调用"

    @staticmethod
    def test_feedback_flip_reattributes_previous_entries():
        """回填翻转上一条结论：对上一条取用过的条目重新归因。"""
        kv = _kv()
        _seed_entry(kv, "e1", "tip: use cp to create config")
        _seed_trajectory(
            kv, "t0", [
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
            ],
            retrieved=[{"id": "e1", "ref": "0.n"}],
        )
        _verdict_in_kv(
            kv, "t0", outcome=OUTCOME_SUCCESS, evidence=EVIDENCE_MODEL_ONLY,
        )
        # 调用序：反馈（第 3 步）→ 补做的重新归因 → 当前轨迹的成败判定（第 4 步）。
        llm = ScriptedLLM([
            json.dumps({"previous": "correction", "internal": "none"}),
            json.dumps({
                "attributions": {"e1": "harmful"}, "unrelated_failure": False
            }),
            json.dumps({"outcome": "success"}),
        ])
        unit = _request_unit(
            messages=[
                {"role": "user", "content": "run the deploy"},
                {"role": "assistant", "content": "deployed"},
                {"role": "user", "content": "that was wrong, redo it"},
            ],
            tool_call_count=1, previous="t0",
        )
        _appraiser(kv, llm).appraise([unit])
        payload = _attr_payload(kv, "e1", "t0", 1)
        assert payload["verdict"] == ATTR_HARMFUL, "翻转后对上一条的取用重新归因"
        assert payload["evidence"] == EVIDENCE_USER_FEEDBACK, (
            "重新归因事件带新证据等级"
        )

    @staticmethod
    def test_missing_trajectory_record_skips_post_update():
        """轨迹记录不在时补做整体跳过、不抛错（防御两次读取间的过期竞态）。

        反馈路径上轨迹记录不在时判定更新本身也不会发生（判定反馈的输入要
        上一条的最终回复）；此分支防御的是回填前后两次读取之间记录恰好过期
        的竞态。直接调用以覆盖该分支。
        """
        kv = _kv()
        appraiser = _appraiser(kv, ScriptedLLM())
        from jiuwen_memory.construction.experience import VerdictRecord

        verdict = VerdictRecord(
            outcome=OUTCOME_FAILURE, evidence=EVIDENCE_USER_FEEDBACK,
            revision=1, derived_ids=["e9"],
        )
        _post_verdict_update(
            appraiser, SCOPE, _digest(), "t0", None, verdict, OUTCOME_SUCCESS,
        )
        assert UtilityStore(kv).scan_events(EXP_SCOPE) == [], "无轨迹记录不补做"

    @staticmethod
    def test_post_update_failure_does_not_fail_supplement():
        """补做自身失败（效用存储故障）：判定更新保留，补报请求不失败。"""
        kv = _kv()
        appraiser, appraisal = _settle(
            kv, messages=_retrieved_messages(), tool_call_count=1
        )
        appraiser.finalize(
            [_request_unit()], appraisal, _SettleResult(created_ids=["e9"])
        )
        broken = SimpleNamespace(
            write_reproduction_event=lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("kv down")
            )
        )
        _inject_broken_utility(appraiser, broken)
        again = appraiser.appraise([_request_unit(tool_call_count=1, external=0.0)])
        assert again.proceed is False, "补报不产出条目"
        updated = _load_verdict(kv, "t1")
        assert updated is not None and updated.revision == 1, (
            "判定更新先于补做，不受补做失败影响"
        )
        assert updated.outcome == OUTCOME_FAILURE, "判定已按外部结果翻转"
