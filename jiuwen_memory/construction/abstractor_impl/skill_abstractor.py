# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SkillAbstractor — 技能归纳（mem2.0 §2.5.3-2.5.5）。

把整理后留下的经验条目归纳为技能草稿。四步（§2.5.3）：

1. **分组**：按类别分桶；桶内正文余弦相似度 ≥ 0.6 连边（两条都是条件动作时
   还要求能力名相同），连通成组；超过 50 条的组按相似度再切开。
2. **条件检查**：组内复现事件合并为来源轨迹，查这一组已有的技能 ``prev``，
   五条件（复现下限 / 可靠成功证据 / 有新证据 / 驳回后有新增复现 / 效用
   不低于基线）须同时满足；每轮至多处理 20 组，新证据多的优先。
3. **生成**：每组选至多 5 条来源轨迹（成功的、证据等级高的、新的优先，
   每个用户先各取一条）读出，连同组内经验、已有技能名、``prev`` 的
   SKILL.md 交模型，每组一次调用，按七项分项输出。
4. **校验**：技能名规则（仍重名加序号）、脱敏检查（命中即丢弃）、命令覆盖
   检查（步骤命令 ≥ 一半能在选出轨迹的工具调用中找到）。通过后渲染
   SKILL.md、装成技能草稿返回。

三种输入里只有经验条目经入参 ``units`` 传入；复现事件在 ``abstract`` 内部扫
``/utility/`` 前缀取得，来源轨迹按复现事件所记的键点读——系统特权：只点读
不枚举不写入。草稿自己不写入（由 evolver 落盘），运行摘要的补写除外
（归纳统计补进整理算子写的同一条 run-summary，§2.5.2）。

草稿不去重（``dedup_products = False``）：同一技能的新版与旧版内容天然相近，
语义去重会把新版误判成重复（§2.5.3）；条目 id 由来源经验集合与证据截止时间
算出，同组同批证据幂等。生成是唯一模型步——LLM 或 Embedder 未配置时
``abstract`` 返回空（配置降级不炸轮次，Curator 同款）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.experience import (
    EVIDENCE_EXTERNAL,
    EVIDENCE_USER_FEEDBACK,
    EVIDENCE_WEIGHTS,
    OUTCOME_SUCCESS,
    ReproductionEvent,
    UtilityStore,
    validity,
)
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    MEMORY_KEY_PREFIX,
    ChatMessage,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.abstractor import Abstractor, AbstractorProducer
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.common import resolve_message_store
from jiuwen_memory.construction.experience import (
    RELEASE_AUTO,
    REPRO_UNIT_TRAJECTORY,
    ExecutionTrajectoryRecord,
    ExperienceConfig,
    TrajectoryStore,
    desensitization_hit,
    risky_operation_hit,
    tool_call_digest,
)

if TYPE_CHECKING:
    from jiuwen_memory.construction.router import SpaceNaming

logger = get_logger(__name__)

# -- 取值（§2.5.3 首版） -------------------------------------------------- #

MEMORY_TYPE_SKILL = "skill"
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_NAME_MAX_LEN = 64
GROUP_SIMILARITY = 0.6          # 分组连边的相似度下限
GROUP_SIZE_CAP = 50             # 单组条数上限（超出按相似度再切）
MAX_GROUPS_PER_ROUND = 20       # 每轮处理的组数上限
TRAJECTORIES_PER_GROUP = 5      # 生成时每组选出的来源轨迹上限
MIN_REPRO_USERS = 2             # 复现下限：独立复现用户数（user 计数部署）
MIN_REPRO_TRAJECTORIES = 3      # 复现下限：独立复现轨迹数（轨迹计数部署）
AUTO_REPRO_USERS = 3            # auto 发布「复现更多」：用户数
AUTO_REPRO_TRAJECTORIES = 5     # auto 发布「复现更多」：轨迹数
SUCCESS_RATIO_MIN = 0.5         # 来源轨迹判成功的比例下限
REJECTED_REGAIN_USERS = 2       # 驳回后重新归纳须新增的复现用户数
STRATEGY_VERSION = "0"          # 策略层预留，首版恒为 0
AUTHOR_PRINCIPAL = "system:experience"
MAX_ATTEMPTS = 3                # 生成调用的尝试次数（construction 统一语义）
REPRO_KIND = "repro"            # 效用事件键的 kind 段
RUN_SUMMARY_TAG = "run-summary"
TIP_KEYS = ("condition", "capability", "action")
# RELEASE_* / REPRO_UNIT_* 的取值域定义在 ``construction/experience.py``
# （部署配置的解析处，v3 §2），本模块按本空间的配置取值。

# 步骤命令的提取：反引号片段优先（模型写步骤时命令自然用反引号包裹），
# 无反引号时退到英文 token——纯中文描述性步骤提不出候选，覆盖检查通过。
_COMMAND_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_COMMAND_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\\/=-]*")


# -- 内部数据类 ---------------------------------------------------------- #


@dataclass
class _Round:
    """一次 abstract 调用的上下文（单一空间，per-space 配置已解析）。"""

    scope: Scope          # 来源经验所在空间（技能草稿也落这里，v3 §2.5）
    release: str          # manual / auto
    repro_unit: str       # user / trajectory（auto 已按落点解析到底，§2.4）


@dataclass
class _Group:
    """通过条件检查的一个候选组。"""

    entries: list[MemoryUnit]                 # 组内经验（curate 后的条目）
    events: list[ReproductionEvent]           # 来源轨迹（按 author+tid 去重）
    prev: MemoryUnit | None                   # 这一组已有的技能（版本最大）
    new_evidence: int                         # 排序键：晚于 prev 截止的事件数


@dataclass
class _GeneratedSkill:
    """生成步的分项输出（校验通过前的形态，不落盘）。"""

    name: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)
    scenarios: str = ""
    preconditions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


# ====================================================================== #
# 算子
# ====================================================================== #


class SkillAbstractor(Abstractor):
    """把一组同类经验条目归纳为技能草稿（§2.5.3）。"""

    # 技能草稿不过语义去重（§2.5.3）：同一技能的新旧版内容天然相近，
    # 去重会把新版误判成重复。evolver 读本属性决定直接落盘。
    dedup_products = False

    def __init__(
        self,
        kv,
        *,
        experience: ExperienceConfig,
        embedder: Embedder | None = None,
        llm: LLM | None = None,
        desensitize_identifiers: list[str] | None = None,
        space_naming: SpaceNaming | None = None,
        group_similarity: float = GROUP_SIMILARITY,
        group_size_cap: int = GROUP_SIZE_CAP,
        max_groups: int = MAX_GROUPS_PER_ROUND,
        trajectories_per_group: int = TRAJECTORIES_PER_GROUP,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._utility = UtilityStore(kv)
        self._trajectories = TrajectoryStore(kv)
        self._kv = kv
        self._embedder = embedder
        self._llm = llm
        # 经验配置（v3 §2）：技能沉淀开关与 per-space 的 skill_release /
        # repro_unit 都从 ``curated_spaces`` 取；repro_unit 的 auto 在取用时
        # 按落点解析到底（resolved_repro_unit），判定处只认 user / trajectory。
        self._experience = experience
        self._space_naming = space_naming
        self._identifiers = [str(name) for name in desensitize_identifiers or []]
        self._group_similarity = float(group_similarity)
        self._group_size_cap = int(group_size_cap)
        self._max_groups = int(max_groups)
        self._trajectories_per_group = int(trajectories_per_group)
        self._max_attempts = max(1, int(max_attempts))

    def operator_type(self) -> OperatorType:
        return OperatorType.ABSTRACTOR

    def health(self) -> None:
        return None

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def abstract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        entries = [u for u in units if u.lifecycle.value == "active"]
        if not entries:
            return []
        if self._llm is None or self._embedder is None:
            logger.info("SkillAbstractor: llm/embedder not configured, skip")
            return []
        orgs = {(u.scope.org, u.scope.space) for u in entries}
        if len(orgs) != 1:
            logger.warning("SkillAbstractor: mixed scopes in input, skip")
            return []
        scope = entries[0].scope
        # 技能沉淀开关（v3 §2.5）：curated_spaces 中本空间 skill 为真才归纳；
        # 技能与来源经验在同一空间，草稿落输入条目所在空间。
        curated = self._experience.curated_of(scope.space or "")
        if curated is None or not curated.skill:
            return []
        round_ = _Round(
            scope=scope,
            release=curated.skill_release,
            repro_unit=self._experience.resolved_repro_unit(
                curated.space, self._space_naming
            ),
        )

        events_by_entry = self._load_repro_events(round_.scope)
        skills = self._load_skills(round_.scope)

        candidates: list[_Group] = []
        for group_entries in self._group(entries):
            group = self._check_group(round_, group_entries, events_by_entry, skills)
            if group is not None:
                candidates.append(group)
        candidates.sort(key=lambda g: g.new_evidence, reverse=True)

        drafts: list[MemoryUnit] = []
        experience_only = 0
        for group in candidates[: self._max_groups]:
            draft = self._generate_group(round_, group, skills)
            if draft is None:
                continue
            if int(draft.system_metadata.get("trajectories_read", 0)) == 0:
                experience_only += 1
            drafts.append(draft)

        processed = min(len(candidates), self._max_groups)
        self._append_run_summary(round_.scope, processed, len(drafts), experience_only)
        if drafts:
            logger.info(
                "SkillAbstractor: %d groups considered, %d drafts, %d experience-only",
                processed, len(drafts), experience_only,
            )
        return drafts

    # ------------------------------------------------------------------
    # 第 1 步 分组
    # ------------------------------------------------------------------

    def _group(self, entries: list[MemoryUnit]) -> list[list[MemoryUnit]]:
        """按类别分桶 → 桶内相似度连边 → 连通成组 → 超限切开。"""
        buckets: dict[str, list[MemoryUnit]] = {}
        for unit in entries:
            buckets.setdefault(_category_of(unit), []).append(unit)
        vectors = {
            unit.id: vec
            for unit, vec in zip(entries, self._embedder.embed([u.content for u in entries]))
        }
        groups: list[list[MemoryUnit]] = []
        for bucket in buckets.values():
            if len(bucket) == 1:
                groups.append(bucket)
                continue
            for component in self._components(bucket, vectors):
                groups.extend(self._split_large(component, vectors))
        return [g for g in groups if g]

    def _components(
        self, bucket: list[MemoryUnit], vectors: Mapping[str, list[float]]
    ) -> list[list[MemoryUnit]]:
        """桶内连边（相似度达标且能力名约束通过）+ BFS 连通分量。"""
        n = len(bucket)
        linked = [[False] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                a, b = bucket[i], bucket[j]
                similar = _cosine(vectors[a.id], vectors[b.id]) >= self._group_similarity
                if similar and _capability_ok(a, b):
                    linked[i][j] = linked[j][i] = True
        seen = [False] * n
        components: list[list[MemoryUnit]] = []
        for start in range(n):
            if seen[start]:
                continue
            queue, members = [start], []
            seen[start] = True
            while queue:
                cur = queue.pop()
                members.append(bucket[cur])
                for nxt in range(n):
                    if linked[cur][nxt] and not seen[nxt]:
                        seen[nxt] = True
                        queue.append(nxt)
            components.append(members)
        return components

    def _split_large(
        self, component: list[MemoryUnit], vectors: Mapping[str, list[float]]
    ) -> list[list[MemoryUnit]]:
        """超过 ``group_size_cap`` 的组按与组内首条的相似度降序贪心分片。"""
        if len(component) <= self._group_size_cap:
            return [component]
        anchor = component[0]
        ordered = [anchor, *sorted(
            component[1:],
            key=lambda u: -_cosine(vectors[anchor.id], vectors[u.id]),
        )]
        cap = self._group_size_cap
        return [ordered[i:i + cap] for i in range(0, len(ordered), cap)]

    # ------------------------------------------------------------------
    # 第 2 步 条件检查
    # ------------------------------------------------------------------

    def _check_group(
        self,
        round_: _Round,
        entries: list[MemoryUnit],
        events_by_entry: Mapping[str, list[ReproductionEvent]],
        skills: list[MemoryUnit],
    ) -> _Group | None:
        """五条件须同时满足（§2.5.3）；不满足返回 ``None``（该组本轮不归纳）。"""
        entry_ids = {u.id for u in entries}
        # 来源轨迹：组内各经验的复现事件合起来，按 author+tid 去重。
        trajectories: dict[tuple[str, str], ReproductionEvent] = {}
        for entry_id in entry_ids:
            for event in events_by_entry.get(entry_id, []):
                trajectories.setdefault((event.author, event.trajectory_id), event)
        events = list(trajectories.values())

        # prev：provenance 与组内经验有交集的技能，取版本最大的一版。
        prev: MemoryUnit | None = None
        for skill in skills:
            if set(skill.provenance) & entry_ids:
                if prev is None or _version_of(skill) > _version_of(prev):
                    prev = skill

        # 条件 1 复现下限：独立复现（使用记录不含本组任何一条经验）。
        independent = [e for e in events if not (set(e.retrieved_ids) & entry_ids)]
        if round_.repro_unit == REPRO_UNIT_TRAJECTORY:
            if len(independent) < MIN_REPRO_TRAJECTORIES:
                return None
        elif len({e.author for e in independent}) < MIN_REPRO_USERS:
            return None

        # 条件 2 有可靠的成功证据：≥1 条强证据成功，且成功比例达标。
        successes = [e for e in events if e.outcome == OUTCOME_SUCCESS]
        strong = [
            e for e in successes
            if e.evidence in (EVIDENCE_EXTERNAL, EVIDENCE_USER_FEEDBACK)
        ]
        if not strong:
            return None
        if len(successes) / len(events) < SUCCESS_RATIO_MIN:
            return None

        # 排序键：新证据条数（晚于 prev 证据截止时间的复现事件数；无 prev
        # 时为全部复现事件数）。
        cutoff = _cutoff_of(prev)
        if prev is None:
            new_evidence = len(events)
        else:
            new_evidence = sum(1 for e in events if e.occurred_at > cutoff)
            # 条件 3 有新证据：prev 存在时必须有晚于其截止时间的复现事件。
            if new_evidence == 0:
                return None
            # 条件 4 驳回后有新增复现：prev 被驳回时复现用户数至少多 2 个。
            if prev.lifecycle.value == "forgotten":
                prev_users = int(prev.system_metadata.get("source_users", 0) or 0)
                if len({e.author for e in events}) < prev_users + REJECTED_REGAIN_USERS:
                    return None

        # 条件 5 效用不低于基线：组内条目合计有效性 ≥ 同类别中位数。
        if not self._utility_ok(round_.scope, entries):
            return None

        return _Group(entries=entries, events=events, prev=prev, new_evidence=new_evidence)

    def _utility_ok(self, scope: Scope, entries: list[MemoryUnit]) -> bool:
        """效用不低于基线（§2.5.3）；无数据（无基线或全组无统计）视为满足。"""
        category = _category_of(entries[0])
        baseline_map = self._utility.load_baseline(scope)
        if category not in baseline_map:
            return True
        baseline = float(baseline_map[category])
        total, counted = 0.0, 0
        for unit in entries:
            stats = self._utility.load_stats(scope, unit.id)
            if stats is None:
                continue
            total += validity(stats, baseline)
            counted += 1
        if counted == 0:
            return True
        return total >= baseline

    # ------------------------------------------------------------------
    # 第 3 步 生成
    # ------------------------------------------------------------------

    def _generate_group(
        self, round_: _Round, group: _Group, skills: list[MemoryUnit]
    ) -> MemoryUnit | None:
        """读轨迹 → 幂等检查 → 模型生成 → 校验 → 渲染 → 装配草稿。"""
        # 幂等（§2.5.3）：条目 id 由来源经验集合与证据截止时间算出，同组
        # 同批证据已存在时不再产出。放在读轨迹与模型调用之前。
        evidence_cutoff = max((e.occurred_at for e in group.events), default="")
        unit_id = _draft_id([u.id for u in group.entries], evidence_cutoff)
        if self._skill_exists(round_.scope, unit_id):
            logger.info("SkillAbstractor: draft %s already exists, skip", unit_id[:16])
            return None

        picks = self._pick_trajectories(group.events)
        records: list[tuple[ReproductionEvent, ExecutionTrajectoryRecord | None]] = []
        for event in picks:
            records.append((event, self._trajectories.load(
                _scope_of(event), event.author, event.trajectory_id,
            )))
        trajectories_read = sum(1 for _, record in records if record is not None)

        existing_names = {name for name in (_skill_name_of(s) for s in skills) if name}
        generated = self._generate_by_llm(group, records, existing_names)
        if generated is None:
            return None

        name = _unique_skill_name(generated.name, existing_names)
        if name is None:
            logger.info("SkillAbstractor: invalid skill name %r, drop", generated.name)
            return None

        content = _render_skill_md(generated, name)
        if self._identifiers and desensitization_hit(content, self._identifiers):
            logger.info("SkillAbstractor: desensitization hit, drop %s", name)
            return None
        if not _command_coverage_ok(generated.steps, records, group.entries):
            logger.info("SkillAbstractor: command coverage check failed, drop %s", name)
            return None

        tag = "draft"
        if round_.release == RELEASE_AUTO and self._auto_ok(
            round_, group, content, trajectories_read
        ):
            tag = "trial"
        return _draft_unit(
            round_=round_,
            group=group,
            unit_id=unit_id,
            name=name,
            content=content,
            tag=tag,
            evidence_cutoff=evidence_cutoff,
            trajectories_read=trajectories_read,
            tools=generated.tools,
        )

    def _pick_trajectories(
        self, events: list[ReproductionEvent]
    ) -> list[ReproductionEvent]:
        """选至多 N 条来源轨迹：成功的、证据等级高的、新的优先，每个用户
        先各取一条（§2.5.3），再按序补足。"""
        ordered = sorted(events, key=_trajectory_rank, reverse=True)
        limit = self._trajectories_per_group
        picked: list[ReproductionEvent] = []
        picked_keys: set[tuple[str, str]] = set()
        seen_authors: set[str] = set()
        for event in ordered:
            if event.author in seen_authors:
                continue
            picked.append(event)
            picked_keys.add((event.author, event.trajectory_id))
            seen_authors.add(event.author)
        for event in ordered:
            if len(picked) >= limit:
                break
            key = (event.author, event.trajectory_id)
            if key in picked_keys:
                continue
            picked.append(event)
            picked_keys.add(key)
        return picked[:limit]

    def _generate_by_llm(
        self,
        group: _Group,
        records: list[tuple[ReproductionEvent, ExecutionTrajectoryRecord | None]],
        existing_names: set[str],
    ) -> _GeneratedSkill | None:
        """每组一次模型调用，按七项分项输出；凑不出可执行流程返回 ``None``。"""
        prompt = _build_prompt(group, records, existing_names)
        messages = [ChatMessage(role="user", content=prompt)]
        for _attempt in range(self._max_attempts):
            try:
                response = self._llm.chat(messages, temperature=0, max_tokens=4096)
            except Exception as exc:  # noqa: BLE001 —— 生成失败重试，见统一尝试次数语义
                logger.warning("SkillAbstractor: llm chat failed: %s", exc)
                continue
            parsed = _parse_llm_json(response)
            if parsed is None:
                logger.info("SkillAbstractor: llm response not valid JSON, retry")
                continue
            skill = _GeneratedSkill(
                name=str(parsed.get("name", "")).strip(),
                description=str(parsed.get("description", "")).strip(),
                tools=[str(t) for t in parsed.get("tools", []) or []],
                scenarios=str(parsed.get("scenarios", "")).strip(),
                preconditions=[str(p) for p in parsed.get("preconditions", []) or []],
                steps=[str(s) for s in parsed.get("steps", []) or []],
                failures=[str(f) for f in parsed.get("failures", []) or []],
            )
            # 名字与步骤是可执行技能的最小集；模型输出空对象表示凑不出。
            if not skill.name or not skill.steps:
                return None
            return skill
        logger.warning(
            "SkillAbstractor: generation failed after %d attempts", self._max_attempts
        )
        return None

    def _auto_ok(
        self, round_: _Round, group: _Group, content: str, trajectories_read: int
    ) -> bool:
        """auto 发布的附加条件（§2.5.5）：复现更多、读到了轨迹、无危险操作。"""
        if trajectories_read < 1:
            return False
        if risky_operation_hit(content) is not None:
            return False
        entry_ids = {u.id for u in group.entries}
        independent = [e for e in group.events if not (set(e.retrieved_ids) & entry_ids)]
        if round_.repro_unit == REPRO_UNIT_TRAJECTORY:
            return len(independent) >= AUTO_REPRO_TRAJECTORIES
        return len({e.author for e in independent}) >= AUTO_REPRO_USERS

    # ------------------------------------------------------------------
    # 输入读取（系统特权：只点读复现事件所记的键）
    # ------------------------------------------------------------------

    def _load_repro_events(
        self, scope: Scope
    ) -> dict[str, list[ReproductionEvent]]:
        """扫经验空间 ``/utility/`` 前缀，只取复现事件，按条目 id 分桶。"""
        by_entry: dict[str, list[ReproductionEvent]] = {}
        for key, payload in self._utility.scan_events(scope):
            if key.kind != REPRO_KIND:
                continue
            event = ReproductionEvent(
                source_scope=[str(x) for x in payload.get("source_scope", []) or []],
                trajectory_id=str(payload.get("trajectory_id", "")),
                author=str(payload.get("author", "")),
                retrieved_ids=[str(x) for x in payload.get("retrieved_ids", []) or []],
                outcome=str(payload.get("outcome", "")),
                evidence=str(payload.get("evidence", "")),
                occurred_at=str(payload.get("occurred_at", "")),
            )
            if event.author and event.trajectory_id:
                by_entry.setdefault(key.entry_id, []).append(event)
        return by_entry

    def _load_skills(self, scope: Scope) -> list[MemoryUnit]:
        """读本空间的全部技能条目（prev 查找、重名检查、幂等检查共用）。

        技能与来源经验在同一空间（v3 §2.5），扫描空间即输入条目所在空间。
        """
        skills: list[MemoryUnit] = []
        for _key, raw in self._kv.scan(scope, MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is None:
                continue
            if str(unit.system_metadata.get("memory_type", "")) == MEMORY_TYPE_SKILL:
                skills.append(unit)
        return skills

    def _skill_exists(self, scope: Scope, unit_id: str) -> bool:
        try:
            self._kv.get(scope, memory_key(unit_id))
        except NotFoundError:
            return False
        return True

    # ------------------------------------------------------------------
    # 运行摘要补写（§2.5.2：归纳统计补进整理算子写的同一条）
    # ------------------------------------------------------------------

    def _append_run_summary(
        self, scope: Scope, groups: int, drafts: int, experience_only: int
    ) -> None:
        """把归纳统计补进最近一条 run-summary（Curator 本轮写的那条）。

        只改本体不建索引（run-summary 本就 ``FORWARD_ONLY``）；找不到摘要
        条目或正文不是 JSON 时跳过并记日志——补写是尽力而为，不阻断归纳
        产物返回。
        """
        latest: MemoryUnit | None = None
        latest_at = ""
        for _key, raw in self._kv.scan(scope, MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is None or RUN_SUMMARY_TAG not in unit.tags:
                continue
            ingested = str(unit.temporal.t_ingest or "")
            if latest is None or ingested > latest_at:
                latest, latest_at = unit, ingested
        if latest is None:
            logger.info("SkillAbstractor: no run-summary entry to append")
            return
        try:
            payload = json.loads(latest.segments[0].content)
        except (ValueError, IndexError):
            logger.warning("SkillAbstractor: run-summary content not JSON, skip append")
            return
        if not isinstance(payload, dict):
            logger.warning("SkillAbstractor: run-summary content not an object, skip append")
            return
        payload["skill_induction"] = {
            "groups": groups,
            "drafts": drafts,
            "experience_only": experience_only,
        }
        latest.segments[0].content = json.dumps(payload, ensure_ascii=False)
        try:
            self._kv.update(scope, memory_key(latest.id), dumps(latest))
        except NotFoundError:
            logger.warning("SkillAbstractor: run-summary entry vanished, skip append")


# ====================================================================== #
# 模块级小件
# ====================================================================== #


def _category_of(unit: MemoryUnit) -> str:
    """条目的类别（经验条目落盘时写进 system_metadata["category"]，§2.3）。"""
    return str(unit.system_metadata.get("category", "")).strip()


def _capability_of(unit: MemoryUnit) -> str:
    """条件动作的能力名（三段的 capability 段）；非条件动作返回空串。"""
    return str(unit.system_metadata.get("capability", "")).strip()


def _capability_ok(a: MemoryUnit, b: MemoryUnit) -> bool:
    """连边的附加约束：两条都是条件动作时还要求能力名相同（§2.5.3）。"""
    cap_a, cap_b = _capability_of(a), _capability_of(b)
    if cap_a and cap_b:
        return cap_a == cap_b
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（任一零向量返回 0，即永不相似）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _trajectory_rank(event: ReproductionEvent) -> tuple[int, float, str]:
    """轨迹选取的排序键：成功的、证据等级高的、新的优先（降序用）。"""
    return (
        1 if event.outcome == OUTCOME_SUCCESS else 0,
        EVIDENCE_WEIGHTS.get(event.evidence, 0.0),
        event.occurred_at,
    )


def _scope_of(event: ReproductionEvent) -> Scope:
    """复现事件记的来源轨迹写入落点（五元组）→ ``Scope``。"""
    parts = [str(x) for x in event.source_scope]
    parts = (parts + [""] * 5)[:5]
    org, space, user, agent, session = parts
    return Scope(org=org, space=space, user=user, agent=agent, session=session)


def _version_of(skill: MemoryUnit) -> int:
    """技能条目的版本号（元数据 ``skill_version``，缺省 1）。"""
    try:
        return int(skill.system_metadata.get("skill_version", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _skill_name_of(skill: MemoryUnit) -> str:
    return str(skill.system_metadata.get("skill_name", "")).strip()


def _cutoff_of(prev: MemoryUnit | None) -> str:
    """prev 的证据截止时间（元数据 ``evidence_cutoff``，缺省空串即全部算新）。"""
    if prev is None:
        return ""
    return str(prev.system_metadata.get("evidence_cutoff", "")).strip()


def _draft_id(entry_ids: list[str], evidence_cutoff: str) -> str:
    """草稿条目 id：来源经验集合与证据截止时间的确定性哈希（§2.5.3 幂等）。"""
    payload = "\x00".join([*sorted(entry_ids), evidence_cutoff])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"skill-{digest}"


def _unique_skill_name(raw: str, existing_names: set[str]) -> str | None:
    """技能名规则（§2.5.4）：小写字母数字连字符 ≤ 64；仍重名的加序号。

    先做最小规范化（小写、下划线转连字符、去首尾连字符），不符合字符集
    或超长的丢弃（返回 ``None``，该组本轮不产出）。
    """
    name = str(raw or "").strip().lower().replace("_", "-").strip("-")
    if not name or len(name) > SKILL_NAME_MAX_LEN or not SKILL_NAME_RE.match(name):
        return None
    if name not in existing_names:
        return name
    for index in range(2, 100):
        candidate = f"{name}-{index}"
        if len(candidate) <= SKILL_NAME_MAX_LEN and candidate not in existing_names:
            return candidate
    return None


def _parse_llm_json(response: str) -> dict | None:
    """解析模型输出：直接 ``json.loads``，失败剥 ``` 围栏再试；空对象是合法输出。"""
    text = str(response or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _step_commands(steps: list[str]) -> list[str]:
    """提取步骤中的候选命令：反引号片段优先，无则退到英文 token。"""
    commands: list[str] = []
    for step in steps:
        text = str(step)
        found = [m.strip() for m in _COMMAND_BACKTICK_RE.findall(text) if m.strip()]
        if not found:
            found = [w for w in _COMMAND_WORD_RE.findall(text) if len(w) >= 2]
        commands.extend(found)
    return commands


def _command_coverage_ok(
    steps: list[str],
    records: list[tuple[ReproductionEvent, ExecutionTrajectoryRecord | None]],
    entries: list[MemoryUnit],
) -> bool:
    """命令覆盖检查（§2.5.3）：步骤命令 ≥ 一半能在证据中找到。

    证据优先取选出轨迹的工具调用（``(工具名, 主要参数)``）；一组选出的
    轨迹都读不到时，改与组内经验的执行动作比对（条件动作的 action 段与
    正文）。提取不出候选命令（纯描述性步骤）时无从检查，视为通过。
    """
    commands = _step_commands(steps)
    if not commands:
        return True
    evidence: list[tuple[str, str]] = []
    for _event, record in records:
        if record is not None:
            evidence.extend(tool_call_digest(record.messages))
    if not evidence:
        for unit in entries:
            metadata = unit.system_metadata
            action = str(metadata.get("action", "")).strip()
            if action:
                evidence.append(("", action))
            if unit.content:
                evidence.append(("", unit.content))
    hits = sum(1 for command in commands if _command_found(command, evidence))
    return hits * 2 >= len(commands)


def _command_found(command: str, evidence: list[tuple[str, str]]) -> bool:
    """候选命令命中证据：等于工具名，或是某次调用的主要参数的子串。"""
    for name, primary in evidence:
        if command == name:
            return True
        if primary and command in primary:
            return True
    return False


def _render_skill_md(skill: _GeneratedSkill, name: str) -> str:
    """渲染 SKILL.md（§2.5.4）：YAML 头 + 正文四节。

    YAML 标量用 ``json.dumps`` 转义——JSON 双引号标量是合法 YAML，且与
    ``yaml.safe_load`` 互认；不引入 yaml 依赖，也不依赖模型输出可直接嵌入。
    """
    lines = [
        "---",
        f"name: {json.dumps(name, ensure_ascii=False)}",
        f"description: {json.dumps(skill.description, ensure_ascii=False)}",
        "tools:",
    ]
    for tool in skill.tools:
        lines.append(f"  - {json.dumps(str(tool), ensure_ascii=False)}")
    lines.extend([
        "---",
        "",
        f"# {name}",
        "",
        "## 适用场景",
        skill.scenarios or skill.description,
        "",
        "## 前置条件",
    ])
    lines.extend(f"- {item}" for item in (skill.preconditions or ["（无）"]))
    lines.extend(["", "## 步骤"])
    lines.extend(f"{index}. {step}" for index, step in enumerate(skill.steps, 1))
    lines.extend(["", "## 常见失败与处置"])
    lines.extend(f"- {item}" for item in (skill.failures or ["（暂无）"]))
    return "\n".join(lines) + "\n"


def _draft_unit(
    *,
    round_: _Round,
    group: _Group,
    unit_id: str,
    name: str,
    content: str,
    tag: str,
    evidence_cutoff: str,
    trajectories_read: int,
    tools: list[str],
) -> MemoryUnit:
    """装配技能草稿条目（§2.5.4 的字段清单；落盘由 evolver 完成）。"""
    now = datetime.now(UTC)
    successes = sum(1 for e in group.events if e.outcome == OUTCOME_SUCCESS)
    return MemoryUnit(
        id=unit_id,
        # 技能草稿落来源经验所在空间（v3 §2.5：不另建空间、不需要配对）。
        scope=round_.scope,
        tier=MemoryTier.PROCEDURAL,
        segments=[Segment(content=content, source=Modality.TEXT)],
        provenance=[u.id for u in group.entries],
        tags=[tag],
        lifecycle=LifecycleState.ACTIVE,
        system_metadata={
            "memory_type": MEMORY_TYPE_SKILL,
            "skill_name": name,
            "skill_version": _version_of(group.prev) + 1 if group.prev else 1,
            "category": _category_of(group.entries[0]),
            "capabilities": [str(t) for t in tools],
            "source_users": len({e.author for e in group.events}),
            "source_trajectories": len(group.events),
            "source_successes": successes,
            "trajectories_read": trajectories_read,
            "evidence_cutoff": evidence_cutoff,
            "induction_batch": _induction_batch(now),
            "strategy_version": STRATEGY_VERSION,
            "author_principal": AUTHOR_PRINCIPAL,
            "author_agent": "",
        },
        temporal=Temporal(t_ingest=now, t_valid=now),
    )


def _induction_batch(now: datetime) -> str:
    """归纳批次标签：ISO 周号（如 ``2026w41``），审核人按批次筛选（§2.5.4）。"""
    calendar = now.isocalendar()
    return f"{calendar[0]}w{calendar[1]:02d}"


def _build_prompt(
    group: _Group,
    records: list[tuple[ReproductionEvent, ExecutionTrajectoryRecord | None]],
    existing_names: set[str],
) -> str:
    """生成步的提示词：组内经验 + 选出轨迹 + 已有技能名 + prev 的 SKILL.md。"""
    entries_payload = []
    for unit in group.entries:
        metadata = unit.system_metadata
        item = {"id": unit.id, "content": unit.content, "category": _category_of(unit)}
        tip = {key: str(metadata.get(key, "")).strip() for key in TIP_KEYS}
        if any(tip.values()):
            item["tip"] = tip
        entries_payload.append(item)

    trajectories_payload = []
    for index, (event, record) in enumerate(records, 1):
        item = {
            "key": f"t{index}",
            "outcome": event.outcome,
            "evidence": event.evidence,
        }
        if record is not None:
            # 拆出 digest 变量让推导式收进单行（G.EXP.04 多行子句）。
            digest = tool_call_digest(record.messages)
            item["tool_calls"] = [[name, primary] for name, primary in digest]
        else:
            item["tool_calls"] = None  # 轨迹已过期，仅凭经验归纳
        trajectories_payload.append(item)

    sections = [
        "你是技能归纳器。给你一组同类经验条目和它们来源轨迹的工具调用记录，"
        "请归纳出一份可复用的技能。",
        "",
        "## 组内经验",
        json.dumps(entries_payload, ensure_ascii=False),
        "",
        "## 来源轨迹（判定结论、证据等级、工具调用序列；null 表示轨迹已过期）",
        json.dumps(trajectories_payload, ensure_ascii=False),
        "",
        "## 技能空间已有技能名（新技能不得与之重名）",
        json.dumps(sorted(existing_names), ensure_ascii=False),
    ]
    if group.prev is not None:
        sections.extend([
            "",
            "## 这组经验已有版本的 SKILL.md（在它基础上修改，保持名称语义）",
            group.prev.content,
        ])
    sections.extend([
        "",
        "## 输出",
        "只输出一个 JSON 对象，不要输出其他内容：",
        '{"name": "小写字母数字与连字符，不超过64字符", '
        '"description": "一句话，供 agent 判断何时使用", '
        '"tools": ["所需工具名"], '
        '"scenarios": "适用场景（一段话）", '
        '"preconditions": ["前置条件"], '
        '"steps": ["步骤1", "步骤2"], '
        '"failures": ["常见失败与处置"]}',
        "凑不出可执行流程时输出 {}。",
        "步骤中出现的命令必须来自来源轨迹的工具调用或组内经验，不要编造。",
        "不要在输出中出现具体用户名、路径主目录等主体相关信息。",
    ])
    return "\n".join(sections)


# -- 装配 ---------------------------------------------------------------- #


@AbstractorProducer.register("skill")
def _build(config):
    """装配 SkillAbstractor：KV 与经验评价/整理算子同源（事件、统计、轨迹
    记录都在同一空间的 KV）；LLM 与 Embedder 可选——生成是唯一模型步、
    相似度连边是分组必需，任一缺省时 ``abstract`` 返回空（配置降级）。

    部署配置（v3 §2）：``globals.experience`` 的 ``curated_spaces`` 给出各空间
    的技能开关（``skill`` 为真才归纳）与 ``skill_release`` / ``repro_unit``
    取值；``repro_unit: auto`` 按落点解析到底（写入者自己的空间按轨迹、其余
    按用户，§2.4）。引用了 skill 抽象算子却没有任何空间开启技能属坏配置，
    装配期报错。``desensitize_identifiers`` 与整理算子同款（主体标识清单，
    命中即丢弃草稿）。
    """
    from jiuwen_memory.common.embedder.base import EmbedderProducer
    from jiuwen_memory.common.errors import ValidationError
    from jiuwen_memory.common.llm.base import LlmProducer
    from jiuwen_memory.construction.experience import (
        parse_experience_config,
        space_naming_of,
    )

    experience = parse_experience_config(config.get("experience"))
    if experience is None:
        raise ValidationError(
            "globals.experience 未配置：经验 profile 装配失败（经验沉淀以配置了"
            "这一项为前提，v3 §2）"
        )
    if not experience.skill_space_names:
        raise ValidationError(
            "skill abstractor 引用时 globals.experience.curated_spaces 须有"
            "至少一个空间声明 skill: true"
        )
    identifiers = config.get("desensitize_identifiers")
    return SkillAbstractor(
        resolve_message_store(config),
        experience=experience,
        embedder=EmbedderProducer.dep(config) if config.get("embedder") is not None else None,
        llm=LlmProducer.dep(config) if config.get("llm") is not None else None,
        desensitize_identifiers=(
            [str(name) for name in identifiers] if identifiers else None
        ),
        space_naming=space_naming_of(config),
        max_attempts=config.get("skill_max_attempts", MAX_ATTEMPTS),
    )
