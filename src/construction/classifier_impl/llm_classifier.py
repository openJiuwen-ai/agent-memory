"""LLMClassifier — M2 多维分类实现（规则优先 + LLM 深度补充）。

四阶段流水线（接口契约见 docs/specs/S05-construction.md Classifier 节）：
  Phase 1  特征抽取
           · FeatureExtractor.extract_batch() → keywords + entities + labels
                │
                ▼
  Phase 2  规则分类（快速通道）
           · 对每个维度优先用规则判定（tier/topic/importance/confidence/freshness）
           · 标记各维度的判定来源（"rule" / "default"）
                │
                ▼
  Phase 3  LLM 分类（深度通道，可选）
           · 仅对规则无法覆盖或规则判定置信度低的维度
           · LLM prompt 要求对规则已覆盖的维度输出 null（保留规则结果）
           · 标记判定来源（"llm"）
                │
                ▼
  Phase 4  结果合并与写入
           · LLM 结果覆盖规则结果，规则作为兜底
           · 各维度结果写入 unit 的 tier / tags / metadata 字段
           · 返回更新后的 unit 列表

五维分类体系：
  ① Tier    认知角色 — provenance映射 > 关键词触发 > source映射 > LLM语义 > 默认EPISODIC
  ② Topic   主题标注 — FeatureExtractor关键词 + 规则映射 + LLM标注
  ③ Importance 重要度 — baseline(source) + keyword_boost + provenance_depth + llm_adjustment
  ④ Confidence 置信度 — 直接陈述0.9 / 强推断0.7 / 弱推断0.5 / 不确定0.3
  ⑤ Freshness 时效性 — hot(7d内) / warm(30d内) / cold(超过30d)

核心原则：规则优先，~80% unit 可由规则快速分类；LLM 只补充少数模糊场景。
纯函数：不落盘、不标记、不更新非输入字段。原地修改 unit。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List

from common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import (
    FeatureSet,
    MemoryTier,
    MemoryUnit,
    Modality,
)
from construction.classifier import ClassifierProducer

from ..base import OperatorType
from ..classifier import Classifier

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 内部类型（实现层，不暴露到 __init__.py）
# ---------------------------------------------------------------------------


class ClassifyDimension(str, Enum):
    """分类维度。"""

    TIER = "tier"
    TOPIC = "topic"
    IMPORTANCE = "importance"
    CONFIDENCE = "confidence"
    FRESHNESS = "freshness"


class FreshnessLevel(str, Enum):
    """时效性等级。"""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass
class ClassifyResult:
    """单条 unit 的分类结果（中间结构，Phase 2/3→4 流转）。"""

    unit_id: str = ""
    tier: MemoryTier = MemoryTier.EPISODIC
    topics: list[str] = field(default_factory=list)
    importance: float = 0.3
    confidence: float = 0.7
    freshness: FreshnessLevel = FreshnessLevel.WARM
    # 各维度的判定来源："rule" / "llm" / "default"
    source: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 规则配置 — 与设计文档 §3.4.11 一致
# ---------------------------------------------------------------------------

# tier 关键词触发器
_TIER_KEYWORD_TRIGGERS: dict[MemoryTier, dict] = {
    MemoryTier.CORE: {
        "keywords": ("偏好", "习惯", "总是", "每次都", "默认", "喜欢", "不喜欢", "喜爱", "讨厌"),
        "min_match": 1,
    },
    MemoryTier.PROCEDURAL: {
        "keywords": ("步骤", "流程", "做法", "方法", "怎么", "如何", "先", "然后"),
        "min_match": 1,
    },
    MemoryTier.SEMANTIC: {
        "keywords": ("定义", "概念", "原理", "机制", "架构", "确认", "事实"),
        "min_match": 1,
    },
}

# source 模态 → 默认 tier 映射
_TIER_SOURCE_MAP: dict[Modality, MemoryTier] = {
    Modality.TEXT: MemoryTier.EPISODIC,
    Modality.CODE: MemoryTier.PROCEDURAL,
    Modality.DOCUMENT: MemoryTier.SEMANTIC,
}

# source 模态 → importance baseline
_IMPORTANCE_SOURCE_BASELINE: dict[Modality, float] = {
    Modality.TEXT: 0.3,
    Modality.CODE: 0.6,
    Modality.DOCUMENT: 0.5,
}

# importance boost/suppress 关键词
_IMPORTANCE_BOOST_KEYWORDS = ("偏好", "核心", "重要", "关键", "必须", "不能", "喜欢", "讨厌")
_IMPORTANCE_SUPPRESS_KEYWORDS = ("随便", "试试", "可能", "大概", "临时", "或许")

# 主题关键词映射（简化版，完整版可从配置加载）
_TOPIC_KEYWORD_MAP: dict[str, tuple] = {
    "技术": (
        "Python",
        "TypeScript",
        "React",
        "API",
        "数据库",
        "后端",
        "前端",
        "编译",
        "调试",
        "部署",
    ),
    "技术/AI": ("机器学习", "模型", "训练", "推理", "GPU", "向量", "embedding"),
    "生活": ("健康", "运动", "饮食", "睡眠", "旅行", "天气", "咖啡", "拿铁", "美式"),
    "工作": ("会议", "报告", "项目", "排期", "需求", "设计", "评审", "里程碑", "团队"),
    "情感/偏好": ("喜欢", "讨厌", "开心", "焦虑", "不满", "满意", "偏好"),
}


# ---------------------------------------------------------------------------
# LLM prompt — Phase 3 深度分类
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM_PROMPT = """\
Classify each memory unit along five dimensions.
Output ONLY a JSON array. No explanation, no markdown fences.

Dimensions:
1. "tier": cognitive role — one of:
   "working", "core", "episodic", "semantic", "procedural", "archival"
   - "core": high-value portrait/preferences that should always stay in context
   - "episodic": events and experiences (what happened)
   - "semantic": facts, concepts, definitions (what is known)
   - "procedural": skills, methods, workflows (how to do things)
   - "working": currently active task/session state
   - "archival": low-frequency long-tail archival
2. "topics": list of topic tags (e.g. ["技术", "技术/AI", "工作"])
3. "importance": 0.0~1.0 — how critical this memory is for the user
4. "confidence": 0.0~1.0 — how certain the information is
   (1.0=directly stated, 0.7=strongly inferred, 0.5=weakly inferred,
   0.3=uncertain)
5. "freshness": one of "hot" (within 7 days), "warm" (within 30 days), "cold" (older than 30 days)

Rules:
- For any dimension where the rule-based result is already clear and confident,
  output null to preserve the rule result.
- Only fill in dimensions where rules are ambiguous or insufficient.
- If no meaningful classification can be made beyond defaults, output null for that dimension.
- "importance" adjustment should be modest (±0.1 from baseline).

Output schema:
[{
  "unit_id": "id of the unit",
  "tier": "episodic" | "semantic" | ... | null,
  "topics": ["topic1", "topic2"] | null,
  "importance": 0.0~1.0 | null,
  "confidence": 0.0~1.0 | null,
  "freshness": "hot" | "warm" | "cold" | null
}]
"""

_UNIT_CONTEXT_TEMPLATE = """\
---
[Unit: {unit_id}]
Tier (current): {current_tier}
Content: {unit_content}
Source: {source}
Provenance: {provenance}
---
"""


# ---------------------------------------------------------------------------
# LLMClassifier
# ---------------------------------------------------------------------------


class LLMClassifier(Classifier):
    """M2 Classifier：规则优先 + LLM 深度补充，五维分类。"""

    def __init__(
        self,
        llm: LLM,
        feature_extractor: FeatureExtractor,
        # LLM 通道控制
        llm_enabled: bool = True,
        # 规则分类配置
        tier_keyword_triggers: dict | None = None,
        tier_source_map: dict | None = None,
        importance_source_baseline: dict | None = None,
        importance_boost_keywords: tuple | None = None,
        importance_suppress_keywords: tuple | None = None,
        topic_keyword_map: dict | None = None,
        # 时效性阈值
        freshness_hot_days: int = 7,
        freshness_warm_days: int = 30,
        # 批量/重试
        max_units_per_llm_call: int = 10,
        max_units_per_classify: int = 50,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._feature_extractor = feature_extractor
        self._llm_enabled = llm_enabled
        # 规则配置（支持外部注入，默认使用内置规则）
        self._tier_keyword_triggers = tier_keyword_triggers or _TIER_KEYWORD_TRIGGERS
        self._tier_source_map = tier_source_map or _TIER_SOURCE_MAP
        self._importance_source_baseline = importance_source_baseline or _IMPORTANCE_SOURCE_BASELINE
        self._importance_boost_keywords = importance_boost_keywords or _IMPORTANCE_BOOST_KEYWORDS
        self._importance_suppress_keywords = (
            importance_suppress_keywords or _IMPORTANCE_SUPPRESS_KEYWORDS
        )
        self._topic_keyword_map = topic_keyword_map or _TOPIC_KEYWORD_MAP
        self._freshness_hot_days = freshness_hot_days
        self._freshness_warm_days = freshness_warm_days
        self._max_units_per_llm_call = max_units_per_llm_call
        self._max_units_per_classify = max_units_per_classify
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms

    def operator_type(self) -> OperatorType:
        return OperatorType.CLASSIFIER

    def health(self) -> None:
        # 探测 LLM 可用性——若不可用则抛异常
        try:
            self._llm.health()
        except Exception as exc:
            from common.errors import HealthCheckError

            raise HealthCheckError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def classify(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """为一批记忆单元打上五维分类标签，返回更新后的单元。"""
        logger.info("Classifier: received %d units", len(units))
        for u in units:
            logger.info(
                "Classifier: input unit id=%s tier=%s provenance=%s content=%s",
                u.id[:8],
                u.tier.value,
                u.provenance,
                u.content[:200],
            )

        if not units:
            return units

        # 限制输入量
        if len(units) > self._max_units_per_classify:
            logger.warning(
                "Classifier: %d units exceeds max_units_per_classify=%d, truncating",
                len(units),
                self._max_units_per_classify,
            )
            units = units[: self._max_units_per_classify]

        # Phase 1: 特征抽取
        features_map = self._phase1_extract(units)

        # Phase 2: 规则分类
        rule_results = self._phase2_rule_classify(units, features_map)
        logger.info("Classifier: Phase 2 rule results - %d units classified", len(rule_results))
        for uid, rr in rule_results.items():
            logger.info(
                "Classifier: rule result id=%s tier=%s topics=%s importance=%.2f "
                "confidence=%.2f freshness=%s source=%s",
                uid[:8],
                rr.tier.value,
                rr.topics,
                rr.importance,
                rr.confidence,
                rr.freshness.value,
                rr.source,
            )

        # Phase 3: LLM 分类（可选）
        llm_results: dict[str, ClassifyResult] = {}
        if self._llm_enabled:
            try:
                llm_results = self._phase3_llm_classify(units, rule_results)
            except Exception:
                logger.warning("Classifier: LLM classification failed, using pure rule results")

        # Phase 4: 结果合并与写入
        self._phase4_merge_and_write(units, rule_results, llm_results)

        return units

    # ------------------------------------------------------------------
    # Phase 1: 特征抽取
    # ------------------------------------------------------------------

    def _phase1_extract(self, units: List[MemoryUnit]) -> dict[str, FeatureSet]:
        """FeatureExtractor 对每个 unit.content 抽取关键词/实体/标签。"""

        features_map: dict[str, FeatureSet] = {}
        texts = [u.content for u in units if u.content.strip()]

        try:
            features_list = self._feature_extractor.extract_batch(texts)
            idx = 0
            for u in units:
                if u.content.strip():
                    if idx < len(features_list):
                        features_map[u.id] = features_list[idx]
                    idx += 1
        except Exception:
            logger.warning("Classifier: FeatureExtractor unavailable, skipping feature extraction")

        return features_map

    # ------------------------------------------------------------------
    # Phase 2: 规则分类（快速通道）
    # ------------------------------------------------------------------

    def _phase2_rule_classify(
        self,
        units: List[MemoryUnit],
        features_map: dict[str, FeatureSet],
    ) -> dict[str, ClassifyResult]:
        """对每个维度优先用规则判定。"""

        results: dict[str, ClassifyResult] = {}

        for u in units:
            result = ClassifyResult(unit_id=u.id)
            content = u.content
            fs = features_map.get(u.id)

            # --- ① Tier 规则判定 ---
            # 优先级 1: provenance 映射 — 派生 unit 已有 tier → 验证而非重分配
            if u.provenance and u.tier != MemoryTier.EPISODIC:
                result.tier = u.tier
                result.source[ClassifyDimension.TIER.value] = "provenance"
            else:
                # 优先级 2: 关键词触发
                tier_from_keywords = self._rule_tier_by_keywords(content)
                if tier_from_keywords is not None:
                    result.tier = tier_from_keywords
                    result.source[ClassifyDimension.TIER.value] = "rule"
                else:
                    # 优先级 3: source 映射
                    tier_from_source = self._tier_source_map.get(u.source, MemoryTier.EPISODIC)
                    result.tier = tier_from_source
                    result.source[ClassifyDimension.TIER.value] = "default"

            # --- ② Topic 规则判定 ---
            topics_from_rules = self._rule_topic(content, fs)
            if topics_from_rules:
                result.topics = topics_from_rules
                result.source[ClassifyDimension.TOPIC.value] = "rule"
            else:
                result.source[ClassifyDimension.TOPIC.value] = "default"

            # --- ③ Importance 规则判定 ---
            result.importance = self._rule_importance(u, content)
            result.source[ClassifyDimension.IMPORTANCE.value] = "rule"

            # --- ④ Confidence 规则判定 ---
            # 从源 unit metadata 继承，或根据关键词启发式
            existing_confidence = u.metadata.get("confidence", "")
            if existing_confidence:
                try:
                    result.confidence = float(existing_confidence)
                    result.source[ClassifyDimension.CONFIDENCE.value] = "provenance"
                except ValueError:
                    result.confidence = 0.7
                    result.source[ClassifyDimension.CONFIDENCE.value] = "default"
            else:
                result.confidence = self._rule_confidence(content)
                result.source[ClassifyDimension.CONFIDENCE.value] = "rule"

            # --- ⑤ Freshness 规则判定 ---
            result.freshness = self._rule_freshness(u)
            result.source[ClassifyDimension.FRESHNESS.value] = "rule"

            results[u.id] = result

        return results

    def _rule_tier_by_keywords(self, content: str) -> MemoryTier | None:
        """关键词触发判定 tier：匹配数 ≥ min_match → 触发。"""
        # 按优先级检查：CORE > PROCEDURAL > SEMANTIC
        for tier in (MemoryTier.CORE, MemoryTier.PROCEDURAL, MemoryTier.SEMANTIC):
            trigger = self._tier_keyword_triggers.get(tier)
            if trigger is None:
                continue
            keywords = trigger.get("keywords", ())
            min_match = trigger.get("min_match", 1)
            match_count = sum(1 for kw in keywords if kw in content)
            if match_count >= min_match:
                return tier
        return None

    def _rule_topic(self, content: str, fs: FeatureSet | None) -> list[str]:
        """规则 + FeatureExtractor 关键词 → 主题标签。"""
        topics: list[str] = []

        # 规则关键词映射
        for topic, keywords in self._topic_keyword_map.items():
            if any(kw in content for kw in keywords):
                topics.append(topic)

        # FeatureExtractor labels 补充
        if fs and fs.labels:
            sentiment = fs.labels.get("sentiment", "")
            if sentiment == "positive" and "情感/偏好" not in topics:
                topics.append("情感/偏好")

        # 去重
        return list(dict.fromkeys(topics))

    def _rule_importance(self, unit: MemoryUnit, content: str) -> float:
        """计算 importance：baseline + boost/suppress + provenance_depth。"""
        # baseline
        baseline = self._importance_source_baseline.get(unit.source, 0.3)

        # keyword boost/suppress
        boost = 0.0
        if any(kw in content for kw in self._importance_boost_keywords):
            boost += 0.2
        if any(kw in content for kw in self._importance_suppress_keywords):
            boost -= 0.1

        # provenance depth：被引用 → 重要
        provenance_depth = 0.0
        # 在实际系统中 provenance_depth 来自检索统计，这里简化：
        # 有 provenance 说明是派生单元，重要性略高
        if unit.provenance:
            provenance_depth += 0.1 * min(len(unit.provenance), 3)

        # 合计 + clamp
        importance = baseline + boost + provenance_depth
        return max(0.0, min(1.0, importance))

    def _rule_confidence(self, content: str) -> float:
        """启发式置信度：直接陈述词→0.9, 强推断词→0.7, 其余→0.7。"""
        # 直接陈述信号
        direct_keywords = ("确认", "明确", "必须", "肯定", "一定", "always", "never")
        if any(kw in content for kw in direct_keywords):
            return 0.9

        # 弱推断信号
        weak_keywords = ("可能", "大概", "也许", "似乎", "maybe", "perhaps")
        if any(kw in content for kw in weak_keywords):
            return 0.5

        # 默认置信度
        return 0.7

    def _rule_freshness(self, unit: MemoryUnit) -> FreshnessLevel:
        """时间衰减判定时效性：t_event 越近越 hot。"""
        # 从 temporal.t_ingest 计算年龄
        if unit.temporal and unit.temporal.t_ingest:
            try:
                ingest_time = datetime.fromisoformat(unit.temporal.t_ingest)
            except (ValueError, TypeError):
                # 无法解析时间 → 默认 warm
                return FreshnessLevel.WARM

            now = datetime.now(tz=timezone.utc)
            # 确保 ingest_time 有 timezone
            if ingest_time.tzinfo is None:
                ingest_time = ingest_time.replace(tzinfo=timezone.utc)

            age_days = (now - ingest_time).total_seconds() / 86400.0

            if age_days <= self._freshness_hot_days:
                return FreshnessLevel.HOT
            elif age_days <= self._freshness_warm_days:
                return FreshnessLevel.WARM
            else:
                return FreshnessLevel.COLD

        return FreshnessLevel.WARM

    # ------------------------------------------------------------------
    # Phase 3: LLM 分类（深度通道）
    # ------------------------------------------------------------------

    def _phase3_llm_classify(
        self,
        units: List[MemoryUnit],
        rule_results: dict[str, ClassifyResult],
    ) -> dict[str, ClassifyResult]:
        """LLM 对规则结果做深度补充：对规则未覆盖或置信度低的维度修正。"""

        # 筛选需要 LLM 补充的 unit：
        # - 规则 tier 来源为 "default" → 需 LLM 判定
        # - 规则 topic 为空 → 需 LLM 标注
        # - 规则 importance < 0.5 或来源为 "default" → 需 LLM 调整
        units_need_llm = []
        for u in units:
            rr = rule_results.get(u.id)
            if rr is None:
                units_need_llm.append(u)
                continue
            # 判断是否需要 LLM 补充
            needs_llm = False
            if rr.source.get(ClassifyDimension.TIER.value) == "default":
                needs_llm = True
            if not rr.topics:
                needs_llm = True
            if rr.source.get(ClassifyDimension.IMPORTANCE.value) == "default":
                needs_llm = True
            if rr.source.get(ClassifyDimension.CONFIDENCE.value) == "default":
                needs_llm = True
            if needs_llm:
                units_need_llm.append(u)

        if not units_need_llm:
            return {}

        # 分批 LLM 调用
        llm_results: dict[str, ClassifyResult] = {}
        batches = []
        for i in range(0, len(units_need_llm), self._max_units_per_llm_call):
            batch_end = i + self._max_units_per_llm_call
            batches.append(units_need_llm[i:batch_end])

        for batch in batches:
            try:
                batch_results = self._llm_classify_one_batch(batch, rule_results)
                llm_results.update(batch_results)
            except Exception:
                logger.warning(
                    "Classifier: LLM batch failed (%d units), skipping LLM for this batch",
                    len(batch),
                )
                continue

        return llm_results

    def _llm_classify_one_batch(
        self,
        units: List[MemoryUnit],
        rule_results: dict[str, ClassifyResult],
    ) -> dict[str, ClassifyResult]:
        """单批 LLM 分类：构建 prompt → 调 LLM → 解析 → 构建 ClassifyResult。"""

        # 构建 unit context
        context_parts = []
        for u in units:
            rr = rule_results.get(u.id)
            current_tier = rr.tier.value if rr else u.tier.value
            provenance_str = ",".join(u.provenance) if u.provenance else "none"
            context_parts.append(
                _UNIT_CONTEXT_TEMPLATE.format(
                    unit_id=u.id,
                    current_tier=current_tier,
                    unit_content=u.content,
                    source=u.source.value,
                    provenance=provenance_str,
                )
            )
        unit_context = "\n".join(context_parts)

        # 构建规则结果概览（让 LLM 知道哪些维度已被规则覆盖）
        rule_summary_lines = []
        for u in units:
            rr = rule_results.get(u.id)
            if rr:
                covered = []
                for dim, src in rr.source.items():
                    if src != "default":
                        covered.append(dim)
                rule_summary_lines.append(
                    f"- {u.id}: rule-covered dimensions: {covered if covered else 'none'}"
                )
        rule_summary = (
            "\n".join(rule_summary_lines) if rule_summary_lines else "No rule results available."
        )

        user_text = (
            f"Memory units:\n{unit_context}\n\n"
            f"Rule-based classification (already done):\n{rule_summary}"
        )

        from common.type_def import ChatMessage

        messages = [
            ChatMessage(role="system", content=_CLASSIFY_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_text),
        ]

        response = self._call_llm_with_retry(messages)
        items = self._parse_llm_response(response)

        # 构建 LLM ClassifyResult
        llm_results: dict[str, ClassifyResult] = {}
        for item in items:
            unit_id = item.get("unit_id", "")
            if not unit_id:
                continue

            # 验证 unit_id 在已知列表中
            known_ids = {u.id for u in units}
            if unit_id not in known_ids:
                continue

            result = ClassifyResult(unit_id=unit_id)

            # Tier：LLM 输出非 null 时覆盖规则
            tier_str = item.get("tier")
            if tier_str is not None:
                try:
                    result.tier = MemoryTier(tier_str)
                    result.source[ClassifyDimension.TIER.value] = "llm"
                except ValueError:
                    # LLM 输出无效 tier → 保留规则结果
                    pass

            # Topics：LLM 输出非 null 时覆盖规则
            topics_list = item.get("topics")
            if topics_list is not None and isinstance(topics_list, list):
                result.topics = topics_list
                result.source[ClassifyDimension.TOPIC.value] = "llm"

            # Importance：LLM 输出非 null 时覆盖规则
            importance_val = item.get("importance")
            if importance_val is not None:
                try:
                    result.importance = max(0.0, min(1.0, float(importance_val)))
                    result.source[ClassifyDimension.IMPORTANCE.value] = "llm"
                except (ValueError, TypeError):
                    logger.debug("Classifier: invalid importance ignored: %r", importance_val)

            # Confidence：LLM 输出非 null 时覆盖规则
            confidence_val = item.get("confidence")
            if confidence_val is not None:
                try:
                    result.confidence = max(0.0, min(1.0, float(confidence_val)))
                    result.source[ClassifyDimension.CONFIDENCE.value] = "llm"
                except (ValueError, TypeError):
                    logger.debug("Classifier: invalid confidence ignored: %r", confidence_val)

            # Freshness：LLM 输出非 null 时覆盖规则
            freshness_str = item.get("freshness")
            if freshness_str is not None:
                try:
                    result.freshness = FreshnessLevel(freshness_str)
                    result.source[ClassifyDimension.FRESHNESS.value] = "llm"
                except ValueError:
                    logger.debug("Classifier: invalid freshness ignored: %r", freshness_str)

            llm_results[unit_id] = result

        return llm_results

    # ------------------------------------------------------------------
    # LLM 调用与解析（与 ExtractorImpl/LLMAbstractor 一致）
    # ------------------------------------------------------------------

    def _call_llm_with_retry(self, messages: list) -> str:
        """调用 LLM.chat()，含重试逻辑。"""
        import time

        last_exc = None
        for attempt in range(self._retry_max_retries):
            try:
                return self._llm.chat(messages, temperature=0, max_tokens=4096)
            except Exception as exc:
                last_exc = exc
                if attempt < self._retry_max_retries - 1:
                    wait = self._retry_backoff_ms * (2**attempt) / 1000.0
                    logger.warning(
                        "Classifier: LLM call failed (attempt %d), retrying in %.1fs",
                        attempt + 1,
                        wait,
                    )
                    time.sleep(wait)
        # 所有重试都失败（retry_max_retries <= 0 时未进入循环，last_exc 仍为 None）
        if last_exc is None:
            raise RuntimeError("LLM 调用未执行：retry_max_retries 必须 >= 1")
        raise last_exc

    def _parse_llm_response(self, response: str) -> list[dict]:
        """解析 LLM 返回的 JSON，失败时尝试提取 JSON 核心部分。"""
        # 尝试直接解析
        try:
            parsed = json.loads(response)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.debug("Classifier: direct JSON parse failed, trying stripped JSON")

        # 解析失败：尝试提取 JSON 部分（去除 markdown fences 等噪声）
        cleaned = self._strip_non_json(response)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.warning("Classifier: LLM response not valid JSON, returning empty")
            return []
        return []

    @staticmethod
    def _strip_non_json(text: str) -> str:
        """去除 markdown fences 等噪声，提取 JSON 核心。"""
        s = text.strip()
        if s.startswith("```"):
            lines = s.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            s = "\n".join(lines)
        return s.strip()

    # ------------------------------------------------------------------
    # Phase 4: 结果合并与写入
    # ------------------------------------------------------------------

    def _phase4_merge_and_write(
        self,
        units: List[MemoryUnit],
        rule_results: dict[str, ClassifyResult],
        llm_results: dict[str, ClassifyResult],
    ) -> None:
        """合并规则+LLM 结果（LLM 覆盖规则，规则兜底），写入 unit 字段。"""

        for u in units:
            rr = rule_results.get(u.id, ClassifyResult(unit_id=u.id))
            lr = llm_results.get(u.id)

            # 合并策略：LLM 覆盖规则（仅覆盖 LLM 明确判定的维度）
            merged = self._merge_results(rr, lr)

            # --- 写入 unit 字段 ---
            # tier
            old_tier = u.tier.value
            old_tags = list(u.tags)
            u.tier = merged.tier

            # topics → tags（追加不重复的主题标签）
            for topic in merged.topics:
                if topic not in u.tags:
                    u.tags.append(topic)

            # importance → metadata
            u.metadata["importance"] = str(merged.importance)

            # confidence → metadata
            u.metadata["confidence"] = str(merged.confidence)

            # freshness → metadata
            u.metadata["freshness"] = merged.freshness.value

            # classify_source → metadata（记录各维度来源供审计）
            u.metadata["classify_source"] = json.dumps(merged.source, ensure_ascii=False)

            logger.info(
                "Classifier: final result id=%s tier=%s→%s topics=%s tags=%s→%s "
                "importance=%.2f confidence=%.2f freshness=%s source=%s",
                u.id[:8],
                old_tier,
                u.tier.value,
                merged.topics,
                old_tags,
                u.tags,
                merged.importance,
                merged.confidence,
                merged.freshness.value,
                merged.source,
            )

    def _merge_results(
        self,
        rule_result: ClassifyResult,
        llm_result: ClassifyResult | None,
    ) -> ClassifyResult:
        """合并规则和 LLM 结果：LLM 覆盖规则，规则兜底。"""
        if llm_result is None:
            return rule_result

        merged = ClassifyResult(unit_id=rule_result.unit_id)

        # 对每个维度：LLM 有明确判定（source 中有 "llm"）→ 用 LLM；否则用规则
        for dim in ClassifyDimension:
            dim_key = dim.value
            llm_source = llm_result.source.get(dim_key, "")

            if llm_source == "llm":
                # LLM 明确判定 → 覆盖规则
                merged.source[dim_key] = "llm"
                if dim == ClassifyDimension.TIER:
                    merged.tier = llm_result.tier
                elif dim == ClassifyDimension.TOPIC:
                    merged.topics = llm_result.topics
                elif dim == ClassifyDimension.IMPORTANCE:
                    merged.importance = llm_result.importance
                elif dim == ClassifyDimension.CONFIDENCE:
                    merged.confidence = llm_result.confidence
                elif dim == ClassifyDimension.FRESHNESS:
                    merged.freshness = llm_result.freshness
            else:
                # LLM 未判定 → 保留规则结果
                merged.source[dim_key] = rule_result.source.get(dim_key, "default")
                if dim == ClassifyDimension.TIER:
                    merged.tier = rule_result.tier
                elif dim == ClassifyDimension.TOPIC:
                    # topics: 合并规则+LLM（去重）
                    merged.topics = list(dict.fromkeys(rule_result.topics + llm_result.topics))
                elif dim == ClassifyDimension.IMPORTANCE:
                    merged.importance = rule_result.importance
                elif dim == ClassifyDimension.CONFIDENCE:
                    merged.confidence = rule_result.confidence
                elif dim == ClassifyDimension.FRESHNESS:
                    merged.freshness = rule_result.freshness

        return merged


# -- 注册到 ClassifierProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@ClassifierProducer.register("llm")
def _build(config):
    return LLMClassifier(
        llm=LlmProducer.dep(config, default="echo"),
        feature_extractor=FeatureExtractorProducer.dep(config, default="keyword"),
        llm_enabled=config.get("classifier_llm_enabled", True),
        max_units_per_llm_call=config.get("classifier_max_units_per_llm_call", 10),
        max_units_per_classify=config.get("classifier_max_units_per_classify", 50),
        retry_max_retries=config.get("classifier_retry_max", 3),
        retry_backoff_ms=config.get("classifier_retry_backoff", 1000),
    )
