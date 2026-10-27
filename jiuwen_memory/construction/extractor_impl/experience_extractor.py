# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ExperienceExtractor — 经验记忆的抽取算子（mem2.0 §2.2 八步中的第 6 步）。

从抽取上下文携带的评价结论（``ExtractContext.appraisal``）取轨迹记录与判定，
值得提炼时至多三次模型调用（提炼、修正、分类）产出候选条目；每条候选是一个
:class:`~common.type_def.MemoryUnit`，交给现有流程做落点判定、去重判定与落盘
（第 7 步）。

只提炼观察到成功的做法，失败的教训改写为正向做法；任务未成功且取用过条目时，
针对未奏效的条目另产出至多一条修正后的条件动作。环境事实（fact）与条件动作
（tip）各是一个抽取策略，供去重判定按策略取规则（§2.2 的「去重判定的规则」）。

落盘前的三道闸都在本算子：可执行标记（确定性规则）、脱敏检查（命中即整条丢弃）、
类别写入上限（达上限的类别候选丢弃并记 dropped 计数，§2.5.2）。

条目不属于任何用户：scope 只填组织与经验空间，作者标记固定为系统主体；来源
轨迹不记在条目上，由评价算子的 ``finalize`` 记复现事件（§3.2）。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.experience import OUTCOME_SUCCESS, UtilityStore
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.common.type_def.memory import (
    MEMORY_CLASS_KEY,
    MemoryTier,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import loads as load_unit
from jiuwen_memory.construction.base import ExtractContext, OperatorType
from jiuwen_memory.construction.common import (
    resolve_message_store,
    validate_llm_attempt_policy,
)
from jiuwen_memory.construction.experience import (
    CATEGORY_WRITE_LIMIT,
    MEMORY_TYPE_EXPERIENCE,
    ExperienceAppraisal,
    ExperienceConfig,
    author_principal,
    compress_trace,
    desensitization_hit,
    first_user_message,
    subject_identifiers,
)
from jiuwen_memory.construction.extractor import Extractor, ExtractorProducer
from jiuwen_memory.construction.prompt_registry import PHASE_EXTRACT, PromptRegistry
from jiuwen_memory.construction.prompt_strategy import EXTRACTION_STRATEGY_KEY
from jiuwen_memory.storage.kv import KVStore

logger = get_logger(__name__)

# 一次沉淀的处理时限（秒），与评价算子同一常量语义；从轨迹记录的 started_at 起算。
DEFAULT_PROCESSING_TIME_LIMIT = 150.0

# 经验条目恒定的元数据取值。``MEMORY_TYPE_EXPERIENCE`` 定义在
# ``construction/experience.py``（配置解析与评价算子共用）。
AUTHOR_PRINCIPAL_SYSTEM = "system:experience"
STRATEGY_VERSION_DEFAULT = "0"

STRATEGY_FACT = "experience_fact"   # 环境事实的抽取策略名（去重规则按它取提示词）
STRATEGY_TIP = "experience_tip"     # 条件动作的抽取策略名

# tip 正文模板（m-0107）：三段渲染成一句，供检索；三段另存入元数据。
TIP_TEMPLATE = "{condition}时，用 {capability} {action}"

# 分类表（§2.3）：首版 10 类加 other，取自 agent-core TTSE 的分类表；接入方不
# 提供类别。分类失败或返回值不在表内时归为 other；分类表首版不演进。
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("external-service-automation", "操作外部服务：远程应用、SaaS、API、已连接的账号或云服务"),
    (
        "documents-office-and-records",
        "处理本地文档：PDF、Word、PPT、表格、表单、合同、报告的生成、读取、转换与编辑",
    ),
    ("data-analytics-and-visualization", "数据分析：对数据集计算指标、清洗、建模、出图"),
    (
        "software-engineering-devops",
        "软件工程与运维：修改、测试、构建、迁移、评审代码仓库与开发基础设施",
    ),
    ("security-privacy-and-risk", "安全与风险：安全评估、威胁检测、漏洞处置、取证、加固"),
    ("media-multimodal-and-creative", "媒体处理：音频、视频、图像、图形、3D 资产的转换与理解"),
    ("science-engineering-modeling", "科学与工程建模：领域方程、仿真与分析"),
    ("search-research-and-knowledge", "检索与调研：查找、核实、引用、整理信息"),
    (
        "embodied-simulation-and-interactive-tasks",
        "交互环境：在模拟、具身或游戏类环境中导航、操作对象、改变环境状态",
    ),
    (
        "skill-agent-meta-workflows",
        "agent 自身的建设：构建、测试、评测 skill、agent、提示词、记忆与编排",
    ),
    ("other", "以上均不明确适用"),
)
CATEGORY_NAMES = frozenset(name for name, _ in CATEGORIES)
DEFAULT_CATEGORY = "other"

# 可执行标记的确定性识别（§2.4「风险控制」）：执行动作含命令、代码、URL 或具体
# 参数取值。命中任一模式即视为可执行；可执行条目在第二个用户复现前检索不返回。
_EXECUTABLE_PATTERNS = (
    re.compile(r"https?://"),                                    # URL
    re.compile(r"`[^`]+`"),                                      # 行内代码
    re.compile(  # 文件名后缀
        r"\b[\w.-]+\.(?:py|js|ts|tsx|java|go|rs|c|cpp|h|sh|bash|zsh|"
        r"yaml|yml|json|toml|ini|conf|cfg|xml|md|sql|env)\b"
    ),
    re.compile(r"(?<![\w-])--?[a-zA-Z][\w-]*"),                  # 命令行旗标
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}=(?!/)"),                   # 环境变量赋值
    re.compile(r"&&|\|\||(?<![<>)])>>(?!\()|(?<![<>])>(?!>)"),   # shell 重定向/连接符
    re.compile(
        r"\b(?:cp|mv|rm|mkdir|rmdir|chmod|chown|ln|cat|grep|sed|awk|find|tar|zip|unzip|"
        r"curl|wget|ssh|scp|git|docker|kubectl|helm|make|cmake|pip|pip3|npm|npx|yarn|"
        r"pnpm|python|python3|node|java|javac|go|rustc|cargo|dotnet|bash|sh|zsh)\b"
    ),
)


def is_executable_action(action: str) -> bool:
    """确定性识别执行动作是否含可执行内容（命令、代码、URL、具体参数取值）。"""
    if not action:
        return False
    return any(pattern.search(action) for pattern in _EXECUTABLE_PATTERNS)


# ====================================================================== #
# 提示词：登记在 yml ``prompts.extract`` 段（experience / experience_revise /
# experience_classify），经 PromptRegistry 读取、可运行时替换；未配置时用内置文本。
# ====================================================================== #

_EXTRACT_PROMPT = """\
You distill reusable experience from an agent's task trajectory.

You will be given the task's opening request, the execution trace (tool calls
and their results, including errors), and the task's outcome. Extract:

- "facts": environment facts observed during the task — each one sentence,
  about the environment (files, configs, services, versions), not about what
  the agent did;
- "tips": conditional actions — reusable advice of the form
  {condition, capability, action}: WHEN <condition> holds, use capability
  <capability> to <action>.

Rules:
- Only extract practices that were OBSERVED TO WORK. A failure is experience
  only as a positively-phrased tip about what to do instead (e.g. an error
  fixed by copying an example config yields a tip about that fix, not a fact
  that "the startup failed").
- One matter per item; keep every specific (paths, commands, names) in the
  source's own wording.
- "capability" is the tool or capability name actually used (e.g. "bash").
- Write in the source's language.

Output ONLY a JSON object, no markdown fences:
{"facts": ["one sentence each", ...],
 "tips": [{"condition": "...", "capability": "...", "action": "..."}, ...]}
Return empty lists when nothing qualifies.
"""

_REVISE_PROMPT = """\
You revise an ineffective memory entry into a corrected conditional action.

You will be given memory entries the agent retrieved during a task that did
NOT succeed, how they were judged, and the execution trace showing what
actually happened. Produce AT MOST ONE revised conditional action
{condition, capability, action} that states what to do instead — the condition
under which the old advice misleads, and the action that fits what the trace
actually showed. Keep every specific (paths, commands, names).

If no defensible revision can be made, return an empty list.

Output ONLY a JSON object, no markdown fences:
{"tips": [{"condition": "...", "capability": "...", "action": "..."}]}
"""

_CLASSIFY_PROMPT = """\
You classify experience entries into business categories.

You will be given a numbered list of entries (facts and conditional actions).
Assign each entry exactly one category from the table below, judged by the
task the entry helps with. Do not invent categories.

{table}

Output ONLY a JSON object, no markdown fences:
{{"categories": {{"<entry id>": "<category name>", ...}}}}
Every given entry id must appear exactly once.
"""


def _category_table_text() -> str:
    return "\n".join(f"- {name}: {description}" for name, description in CATEGORIES)


def _prompt(registry: PromptRegistry, key: str, fallback: str) -> str:
    text = registry.get(PHASE_EXTRACT, key)
    return text if text else fallback


@dataclass
class _Draft:
    """一条候选在分类与构造之间的中间形态。"""

    entry_id: str    # 分类调用用的临时 id（e0、e1、…）
    strategy: str    # experience_fact / experience_tip
    content: str     # 渲染后的正文（fact 原句 / tip 模板句）
    category: str = DEFAULT_CATEGORY  # 业务类别（分类调用回填，§2.3）
    condition: str = ""
    capability: str = ""
    action: str = ""


class ExperienceExtractor(Extractor):
    """经验记忆的抽取算子：轨迹沉淀八步中的第 6 步。"""

    def __init__(
        self,
        *,
        llm: LLM,
        kv: KVStore,
        experience: ExperienceConfig,
        prompt_registry: PromptRegistry | None = None,
        category_write_limit: int = CATEGORY_WRITE_LIMIT,
        processing_time_limit: float = DEFAULT_PROCESSING_TIME_LIMIT,
        max_attempts: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._kv = kv
        # 经验配置（v3 §2）：候选落点（default_space 解析）与类别标记
        # （memory_class）都从这一份取。
        self._experience = experience
        self._prompts = prompt_registry or PromptRegistry()
        self._category_write_limit = int(category_write_limit)
        self._time_limit = float(processing_time_limit)
        self._max_attempts, self._retry_backoff_ms = validate_llm_attempt_policy(
            max_attempts, retry_backoff_ms,
        )
        self._utility = UtilityStore(kv)

    # -- 契约 ---------------------------------------------------------------- #

    def operator_type(self) -> OperatorType:
        """返回本算子的类型 ``EXTRACTOR``。"""
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        """探活：检查 LLM。"""
        self._llm.health()

    # -- 第 6 步 -------------------------------------------------------------- #

    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        """从轨迹评价结论中提炼候选条目；不值得提炼或无评价结论时返回空。"""
        exp = _appraisal_data(context)
        if exp is None:
            logger.warning(
                "ExperienceExtractor: no appraisal in context, skip — "
                "经验 profile 的写入必须先经评价算子（§2.2）"
            )
            return []
        if not exp.worth_extracting:
            logger.info(
                "ExperienceExtractor: trajectory not worth extracting, skip, id=%s",
                exp.record.trajectory_id,
            )
            return []
        source = units[0]
        record, verdict = exp.record, exp.verdict
        # 各步开始前检查处理时限（§2.2「耗时与重试」）。
        self._check_deadline(record.started_at, "extract")
        facts, tips = self._extract_entries(record, verdict)
        self._check_deadline(record.started_at, "revise")
        tips = tips + self._revise_entries(
            self._experience_scope_of(source.scope), record, verdict, exp.attribution
        )
        if not facts and not tips:
            return []
        self._check_deadline(record.started_at, "classify")
        drafts = self._classify(facts, tips)
        return self._build_units(source, record, drafts)

    # -- 提炼（模型 1） -------------------------------------------------------- #

    def _extract_entries(
        self, record, verdict
    ) -> tuple[list[str], list[dict[str, str]]]:
        """提炼环境事实与条件动作（只提炼观察到成功的做法）。"""
        prompt = _prompt(self._prompts, "experience", _EXTRACT_PROMPT)
        payload = self._call_llm(prompt, self._trajectory_text(record, verdict))
        facts = _parse_str_list(payload.get("facts"))
        tips = _parse_tip_list(payload.get("tips"))
        logger.info(
            "ExperienceExtractor: distilled %d facts and %d tips, trajectory=%s",
            len(facts), len(tips), record.trajectory_id,
        )
        return facts, tips

    def _trajectory_text(self, record, verdict) -> str:
        """拼提炼 prompt 的输入：开头请求、判定结论、压缩轨迹、能力清单。"""
        parts = [
            f"## Opening request\n{first_user_message(record.messages) or '(none)'}",
            f"## Outcome\n{verdict.outcome}",
            f"## Execution trace\n{compress_trace(record.messages)}",
        ]
        if record.capabilities:
            listing = "\n".join(
                f"- {item.get('name', '')}: {item.get('description', '')}"
                if isinstance(item, dict) else str(item)
                for item in record.capabilities
            )
            parts.append(f"## Available capabilities\n{listing}")
        return "\n\n".join(parts)

    # -- 修正（模型 2，条件执行） ---------------------------------------------- #

    def _revise_entries(
        self, exp_scope: Scope, record, verdict, attribution: dict[str, str]
    ) -> list[dict[str, str]]:
        """任务未成功且取用过条目时，针对未奏效的条目产出至多一条修正后的条件动作。

        ``exp_scope`` 是经验落点空间的 scope（§2.3）：取用过的条目都在这个
        空间。「未奏效」的条目由归因结论给出（helpful 以外）；未提供使用记录
        或全部读不到时不调模型（无从针对）。
        """
        if verdict.outcome == OUTCOME_SUCCESS or not record.retrieved:
            return []
        entries = self._load_retrieved(exp_scope, record, attribution)
        if not entries:
            return []
        prompt = _prompt(self._prompts, "experience_revise", _REVISE_PROMPT)
        listing = "\n\n".join(
            f"[Entry {entry_id}] ({judgement})\n{content}"
            for entry_id, judgement, content in entries
        )
        payload = self._call_llm(
            prompt,
            f"## Task outcome\n{verdict.outcome}（未成功）\n\n"
            f"## Retrieved entries (judged)\n{listing}\n\n"
            f"## Execution trace\n{compress_trace(record.messages)}",
        )
        tips = _parse_tip_list(payload.get("tips"))
        if len(tips) > 1:
            logger.warning(
                "ExperienceExtractor: revise returned %d tips, keeping the first "
                "(至多一条), trajectory=%s",
                len(tips), record.trajectory_id,
            )
            tips = tips[:1]
        return tips

    def _load_retrieved(
        self, exp_scope: Scope, record, attribution: dict[str, str]
    ) -> list[tuple[str, str, str]]:
        """读取用条目的内容并按归因结论排序：「未奏效」（非 helpful）的在前。

        条目在经验落点空间（``exp_scope``，§2.3）。读不到的条目（已删除）
        跳过；全部读不到时返回空。helpful 的条目也带上（给模型完整上下文），
        但未奏效的排在前面。
        """
        loaded: list[tuple[str, str, str, int]] = []
        for item in record.retrieved or []:
            entry_id = str(item.get("id", "")).strip()
            if not entry_id:
                continue
            try:
                unit = load_unit(self._kv.get(exp_scope, memory_key(entry_id)))
            except Exception:
                unit = None
            if unit is None:
                logger.warning(
                    "ExperienceExtractor: retrieved entry missing, skip revise, "
                    "entry=%s", entry_id,
                )
                continue
            judgement = attribution.get(entry_id, "neutral")
            rank = 0 if judgement != "helpful" else 1
            loaded.append((entry_id, judgement, unit.content, rank))
        loaded.sort(key=lambda item: item[3])
        return [(entry_id, judgement, content) for entry_id, judgement, content, _ in loaded]

    # -- 分类（模型 3） -------------------------------------------------------- #

    def _classify(
        self, facts: list[str], tips: list[dict[str, str]]
    ) -> list[_Draft]:
        """对新条目分类（一次模型调用），构造候选的中间形态。"""
        drafts: list[_Draft] = []
        for index, fact in enumerate(facts):
            drafts.append(
                _Draft(
                    entry_id=f"e{index}",
                    strategy=STRATEGY_FACT,
                    content=fact,
                )
            )
        for index, tip in enumerate(tips):
            drafts.append(
                _Draft(
                    entry_id=f"t{index}",
                    strategy=STRATEGY_TIP,
                    content=TIP_TEMPLATE.format(**tip),
                    condition=tip["condition"],
                    capability=tip["capability"],
                    action=tip["action"],
                )
            )
        if not drafts:
            return []
        prompt = _prompt(
            self._prompts, "experience_classify", _CLASSIFY_PROMPT
        ).format(table=_category_table_text())
        listing = "\n".join(f"[{d.entry_id}] {d.content}" for d in drafts)
        payload = self._call_llm(prompt, listing)
        raw = payload.get("categories")
        table = raw if isinstance(raw, dict) else {}
        for draft in drafts:
            category = str(table.get(draft.entry_id, "")).strip()
            if category not in CATEGORY_NAMES:
                if category:
                    logger.warning(
                        "ExperienceExtractor: unknown category %r, fallback %r",
                        category, DEFAULT_CATEGORY,
                    )
                category = DEFAULT_CATEGORY
            draft.category = category
        return drafts

    # -- 构造候选 ------------------------------------------------------------- #

    def _build_units(
        self, source: MemoryUnit, record, drafts: list[_Draft]
    ) -> list[MemoryUnit]:
        """三道闸（可执行标记在渲染时已定、脱敏检查、类别写入上限）后构造候选。

        脱敏命中的候选整条丢弃（不改写）；所属类别的 active 计数已达写入上限的
        候选丢弃并把 dropped 计数记在计数记录上（§2.5.2）。
        """
        identifiers = subject_identifiers(
            user=source.scope.user, agent=source.scope.agent,
            author=author_principal(user=record.user.user, agent=record.user.agent),
        )
        exp_scope = self._experience_scope_of(source.scope)
        units: list[MemoryUnit] = []
        for draft in drafts:
            if desensitization_hit(draft.content, identifiers):
                logger.warning(
                    "ExperienceExtractor: candidate dropped by desensitization, "
                    "trajectory=%s", record.trajectory_id,
                )
                continue
            category = draft.category
            counts = self._utility.load_category_count(exp_scope, category)
            if counts.get("active", 0) >= self._category_write_limit:
                self._utility.add_category_count(exp_scope, category, dropped=1)
                logger.warning(
                    "ExperienceExtractor: category %r at write limit %d, candidate "
                    "dropped, trajectory=%s",
                    category, self._category_write_limit, record.trajectory_id,
                )
                continue
            units.append(self._build_unit(source, draft, category))
        return units

    def _build_unit(self, source: MemoryUnit, draft: _Draft, category: str) -> MemoryUnit:
        """构造一条候选 MemoryUnit；落点为 ``default_space`` 解析的空间（§2.3）。

        带 ``coords`` 的请求在随后的落点判定中按判定表渲染覆盖这里的落点
        （候选已带 ``memory_class``，判定器直接取落点不调模型）；不带时落点
        判定跳过，候选保持这里设的落点。
        """
        now = datetime.now(UTC)
        is_tip = draft.strategy == STRATEGY_TIP
        system_metadata: dict[str, Any] = {
            # 轨迹请求带来的键只沿用 memory_type 与引擎写入的流水线标记（§2.2）。
            "memory_type": str(
                source.system_metadata.get("memory_type", MEMORY_TYPE_EXPERIENCE)
            ),
            "pipeline": str(source.system_metadata.get("pipeline", "")).strip(),
            MEMORY_CLASS_KEY: self._experience.memory_class,
            "category": category,
            "strategy_version": STRATEGY_VERSION_DEFAULT,
            "author_principal": AUTHOR_PRINCIPAL_SYSTEM,
            # 供现有去重判定读取：抽取策略名与去重规则的提示词键（§2.2）。
            EXTRACTION_STRATEGY_KEY: draft.strategy,
            f"_consolidation_prompt_{draft.strategy}": draft.strategy,
        }
        if is_tip:
            system_metadata.update(
                {
                    "condition": draft.condition,
                    "capability": draft.capability,
                    "action": draft.action,
                    "executable": is_executable_action(draft.action),
                }
            )
        return MemoryUnit(
            id=str(uuid.uuid4()),
            # 经验条目不属于任何用户：scope 只填组织与经验落点空间（m-0107）。
            scope=self._experience_scope_of(source.scope),
            tier=MemoryTier.PROCEDURAL if is_tip else MemoryTier.SEMANTIC,
            segments=[Segment(content=draft.content)],
            temporal=Temporal(
                t_event=None,
                t_ingest=now,
                t_valid=now,
                t_message=source.temporal.t_message,
            ),
            # 来源轨迹不记在条目上，记在复现事件上（§3.2）；provenance 仍回指
            # 轨迹请求 unit（血缘列表，可指向未落盘源）。
            provenance=[source.id],
            system_metadata=system_metadata,
        )

    # -- 公共小件 ------------------------------------------------------------- #

    def _experience_scope_of(self, scope: Scope) -> Scope:
        """经验条目的落点 scope：``default_space`` 解析（§2.3）。

        ``own`` 取写入落点（本次写入的 ``MemoryUnit`` 的 scope 所在空间），
        否则取配置的空间名。取用条目的读取与类别计数也在这个空间。
        """
        return Scope(org=scope.org, space=self._experience.experience_space_of(scope))

    def _check_deadline(self, started_at: str, step: str) -> None:
        """各步开始前检查处理时限（从轨迹记录的 started_at 起算）。"""
        try:
            started = datetime.fromisoformat(started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            started = datetime.now(UTC)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if elapsed > self._time_limit:
            raise TimeoutError(
                f"experience processing time limit exceeded at step {step!r}: "
                f"{elapsed:.0f}s > {self._time_limit:.0f}s"
            )

    def _call_llm(self, system_prompt: str, user_text: str) -> dict:
        """一次模型调用（含重试），返回解析后的 JSON object。"""
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_text),
        ]
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._llm.chat(messages, temperature=0, max_tokens=2048)
                return _parse_object(response)
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_attempts - 1:
                    wait = self._retry_backoff_ms * (2**attempt) / 1000.0
                    logger.warning(
                        "ExperienceExtractor: LLM call failed (attempt %d), retry in "
                        "%.1fs", attempt + 1, wait,
                    )
                    time.sleep(wait)
        raise last_exc if last_exc else RuntimeError("LLM call did not execute")


# ====================================================================== #
# 解析小件
# ====================================================================== #


def _appraisal_data(context: ExtractContext | None) -> ExperienceAppraisal | None:
    """从抽取上下文取评价结论；未配置评价算子时为 ``None``。"""
    appraisal = getattr(context, "appraisal", None)
    data = getattr(appraisal, "data", None)
    return data if isinstance(data, ExperienceAppraisal) else None


def _parse_str_list(raw: Any) -> list[str]:
    """解析 fact 列表：逐条 strip，丢弃空串与非字符串。"""
    if not isinstance(raw, list):
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _parse_tip_list(raw: Any) -> list[dict[str, str]]:
    """解析 tip 列表：condition/capability/action 三段齐全才保留。"""
    if not isinstance(raw, list):
        return []
    tips: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tip = {
            key: str(item.get(key, "")).strip()
            for key in ("condition", "capability", "action")
        }
        if all(tip.values()):
            tips.append(tip)
    return tips


def _parse_object(response: str) -> dict:
    """解析模型返回的 JSON object；失败再尝试提取最外层花括号段。

    纯空白响应对应空 object：prompt 允许模型在「无可提炼」时返回空列表，真实
    模型可能直接给出空文本而非 ``{"facts": [], "tips": []}``（e2e 实测 GLM-5.3
    对全失败的轨迹六次一致返回空），语义等同无产出，不作解析错误——下游对缺失
    键的取值就是空列表，提炼因而无候选。
    """
    text = (response or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise
        payload = json.loads(match.group())
    if not isinstance(payload, dict):
        raise ValueError("extract 响应必须是 JSON object")
    return payload


# ====================================================================== #
# 装配
# ====================================================================== #


def _validate_route_table_class(config: Any, memory_class: str) -> None:
    """装配期校验（§2.3）：判定表非空时，经验类别须已声明。

    带 ``coords`` 的请求按判定表中该类别的空间模板渲染落点，未声明的类别
    渲染不出空间。判定表为空时不校验（未开启落点判定的部署按部署配置的
    ``default_space`` 落点）。
    """
    from collections.abc import Mapping

    from jiuwen_memory.construction.router import parse_route_table

    raw = config.get("route_table")
    if not raw or not isinstance(raw, Mapping):
        return
    table = parse_route_table(raw)
    if table.class_of(memory_class) is None:
        raise ValidationError(
            f"route_table 未声明经验类别 {memory_class!r}：经验 profile 装配失败"
            "（带 coords 的请求将无法渲染经验空间）"
        )


@ExtractorProducer.register("experience")
def _build(config):
    """装配 ExperienceExtractor；判定表非空时校验经验类别声明（§2.3）。"""
    from jiuwen_memory.config.config_source import ConfigSourceProducer
    from jiuwen_memory.construction.experience import parse_experience_config

    experience = parse_experience_config(config.get("experience"))
    if experience is None:
        raise ValidationError(
            "globals.experience 未配置：经验 profile 装配失败（经验沉淀以配置了"
            "这一项为前提，v3 §2）"
        )
    _validate_route_table_class(config, experience.memory_class)
    prompts_data = config.get("prompts")
    config_source = ConfigSourceProducer.get_cached("default")
    registry = (
        PromptRegistry.from_dict(prompts_data, config_source=config_source)
        if prompts_data
        else PromptRegistry(config_source=config_source)
    )
    return ExperienceExtractor(
        llm=LlmProducer.dep(config, default="echo"),
        kv=resolve_message_store(config),
        experience=experience,
        prompt_registry=registry,
        category_write_limit=config.get("category_write_limit", CATEGORY_WRITE_LIMIT),
        max_attempts=config.get("extractor_max_attempts", 3),
        retry_backoff_ms=config.get("extractor_retry_backoff", 1000),
    )
