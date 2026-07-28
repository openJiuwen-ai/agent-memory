"""LLMClassifier — 纯 LLM 抽取 tier + tags 的分类实现。

对一批 MemoryUnit，单次 LLM 调用产出每条的 ``tier``（认知角色）与 ``tags``（主题标签），
写回 ``unit.tier`` / ``unit.tags``。不抽派生记忆、不做特征富化、不走规则通道——
仅做 tier+tags 的 LLM 判定（对齐 extractor 的 tier/tags 抽取口径，但 classifier 作用于
原始 unit 而非派生）。

用途：``engine.write`` 在 ``infer=false``（默认路径）时调用 ``classifier.classify``
给原文打 tier+tags；``infer=true`` 路径由 extractor 在派生时一并产出 tier+tags，
不经 classifier。

tier 限定 ``episodic`` / ``semantic`` / ``procedural``（与 extractor 一致），非法值兜底
EPISODIC（原文未分类时的保守默认，区别于派生兜底 SEMANTIC）。tags 1-3 个，清洗截断。
LLM 不可用/解析失败降级为空 tags + EPISODIC tier，不阻断。
"""

from __future__ import annotations

import json
import re
from typing import List

from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import ChatMessage, LifecycleState, MemoryTier, MemoryUnit
from construction.base import OperatorType
from construction.classifier import Classifier, ClassifierProducer
from construction.common import parse_tags

logger = get_logger(__name__)

# 分类阶段允许 LLM 产出的 tier（与 extractor 一致）。
_CLASSIFY_ALLOWED_TIERS: set[str] = {
    MemoryTier.EPISODIC.value,
    MemoryTier.SEMANTIC.value,
    MemoryTier.PROCEDURAL.value,
}

# LLM 抽的 tags 上限。
_MAX_TAGS = 3

# 批量分类的子批大小（单批 LLM 调用上限，超批分多次）。
_CLASSIFY_BATCH_SIZE = 10

_CLASSIFY_SYSTEM_PROMPT = """\
Classify each memory below into its cognitive role (tier) and topic tags.

Output ONLY a JSON array. No explanation, no markdown fences. One entry per input memory,
in the SAME order as input. Each entry:

- "source_id": the bare id of the input memory (the UUID shown in the [ID: ...] marker,
  WITHOUT the wrapper).
- "tier": one of:
  - "episodic": an event/experience that happened at a point in time.
  - "semantic": a stated fact / concept / preference (what is known or liked).
  - "procedural": how-to / skill / process / pattern (how something is done).
  Choose based on the NATURE of the content. When unsure, use "episodic" (raw conversation
  defaults to episodic — it records what happened).
- "tags": 1 to 3 short labels summarizing the memory's topic. Rules: lowercase; drop
  articles/stopwords; same language as the content; keep each tag 1-3 words. Example:
  content "Alice prefers an Americano in the morning" → tags: ["coffee", "preference"].

Output schema:
[{
  "source_id": "<bare uuid>",
  "tier": "episodic" | "semantic" | "procedural",
  "tags": ["tag1", "tag2"]
}]
"""

_SOURCE_PREFIX = """\
---
[ID: {unit_id}]
{unit_content}
---
"""


def _parse_tier(raw) -> MemoryTier:
    """解析 LLM 输出的 tier，限定 episodic/semantic/procedural；非法/缺失兜底 EPISODIC。

    与 extractor 的 _parse_tier 对齐口径，但兜底改为 EPISODIC（原文未分类保守归情景记忆）。
    """
    s = str(raw or "").strip().lower()
    if s in _CLASSIFY_ALLOWED_TIERS:
        return MemoryTier(s)
    if s:
        logger.debug("LLMClassifier._parse_tier: unknown tier %r, fallback EPISODIC", s)
    return MemoryTier.EPISODIC


def _strip_source_id_shell(raw: str) -> str:
    """剥掉 LLM 误带的 [ID: ...] 外壳（同 extractor 兜底）。"""
    m = re.match(r"^\s*\[ID:\s*(?P<id>[^\]]+)\]\s*$", raw, re.IGNORECASE)
    return m.group("id").strip() if m else raw.strip()


class LLMClassifier(Classifier):
    """纯 LLM tier+tags 分类：单次 LLM 调用产出每条 tier/tags，写回 unit。"""

    def __init__(
        self,
        llm: LLM,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms

    def operator_type(self) -> OperatorType:
        return OperatorType.CLASSIFIER

    def health(self) -> None:
        try:
            self._llm.health()
        except Exception as exc:
            from common.errors import HealthCheckError

            raise HealthCheckError(str(exc)) from exc

    def classify(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """对一批 unit 调 LLM 产出 tier+tags，写回 unit.tier/unit.tags。"""
        if not units:
            return units
        # 只分类 ACTIVE 且非空的 unit（派生/失效跳过）
        accepted = [
            u for u in units
            if u.lifecycle == LifecycleState.ACTIVE and u.content.strip()
        ]
        logger.info(
            "LLMClassifier: received %d units, %d accepted (active+non-empty)",
            len(units), len(accepted),
        )
        if not accepted:
            return units

        # 按子批拼 prompt 逐批调用（单批失败不波及其余）
        for start in range(0, len(accepted), _CLASSIFY_BATCH_SIZE):
            sub = accepted[start:start + _CLASSIFY_BATCH_SIZE]
            try:
                self._classify_batch(sub)
            except Exception:
                logger.warning(
                    "LLMClassifier: batch failed offset=%d size=%d, skipping",
                    start, len(sub),
                )
        return units

    def _classify_batch(self, units: List[MemoryUnit]) -> None:
        """单批 LLM 分类：拼 prompt → 调 LLM → 解析 → 回写 tier/tags。"""
        parts = [_SOURCE_PREFIX.format(unit_id=u.id, unit_content=u.content) for u in units]
        user_text = "\n".join(parts)
        messages = [
            ChatMessage(role="system", content=_CLASSIFY_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_text),
        ]
        response = self._call_llm_with_retry(messages)
        items = self._parse_response(response)
        unit_map = {u.id: u for u in units}

        matched = 0
        for item in items:
            sid = _strip_source_id_shell(str(item.get("source_id", "")))
            unit = unit_map.get(sid)
            if unit is None:
                logger.debug("LLMClassifier: source_id %r not in batch, skip", sid)
                continue
            unit.tier = _parse_tier(item.get("tier"))
            tags = parse_tags(item.get("tags"))
            if tags:
                # 合并到已有 tags（追加去重，≤ _MAX_TAGS）
                existing = {t.lower() for t in unit.tags}
                for t in tags:
                    if t.lower() not in existing:
                        unit.tags.append(t)
                        existing.add(t.lower())
                # 截断
                if len(unit.tags) > _MAX_TAGS:
                    unit.tags = unit.tags[:_MAX_TAGS]
            matched += 1
            logger.info(
                "LLMClassifier: unit %s → tier=%s tags=%s",
                unit.id[:8], unit.tier.value, unit.tags,
            )
        logger.info(
            "LLMClassifier: classified %d/%d units (raw items=%d)",
            matched, len(units), len(items),
        )

    def _call_llm_with_retry(self, messages: list) -> str:
        import time

        last_exc = None
        for attempt in range(self._retry_max_retries):
            try:
                return self._llm.chat(messages, temperature=0, max_tokens=4096)
            except Exception as exc:
                last_exc = exc
                if attempt < self._retry_max_retries - 1:
                    wait = self._retry_backoff_ms * (2 ** attempt) / 1000.0
                    logger.warning(
                        "LLMClassifier: LLM call failed (attempt %d), retrying in %.1fs",
                        attempt + 1, wait,
                    )
                    time.sleep(wait)
        if last_exc is None:
            raise RuntimeError("LLMClassifier: LLM 调用未执行（retry_max_retries 必须 >= 1）")
        raise last_exc

    def _parse_response(self, response: str) -> list[dict]:
        """解析 LLM 返回的 JSON 数组（容错 markdown fence/单对象）。"""
        try:
            parsed = json.loads(response.strip())
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            pass
        # 兜底：剥 markdown fence 再试
        cleaned = self._strip_non_json(response)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.warning(
                "LLMClassifier: cannot parse LLM response as JSON, return empty: %s",
                response[:200],
            )
        return []  # 所有解析路径失败，统一返空

    @staticmethod
    def _strip_non_json(text: str) -> str:
        s = text.strip()
        if s.startswith("```"):
            lines = s.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            s = "\n".join(lines)
        return s.strip()


# -- 注册到 ClassifierProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@ClassifierProducer.register("llm")
def _build(config):
    return LLMClassifier(
        llm=LlmProducer.dep(config, default="echo"),
        retry_max_retries=config.get("classifier_retry_max", 3),
        retry_backoff_ms=config.get("classifier_retry_backoff", 1000),
    )
