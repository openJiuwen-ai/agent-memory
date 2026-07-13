"""LLMLayerAnnotator — LLM 版分层标注（background / infer=true 路径用）。

对超阈 content 的 unit 批量调 LLM 生成 L0/L1，回填 ``unit.layers``。任务单一
（仅生成摘要+要点）、prompt 精简，避免 L0/L1 指令混入主抽取 prompt 导致输出超限。

仅对 ``len(content) > layers_threshold`` 的 unit 标注，短 content 留空。失败降级为空
layers，不阻断演进。
"""

from __future__ import annotations

import json
import re
from typing import List

from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import MemoryUnit
from construction.base import OperatorType
from construction.layer_annotator import LayerAnnotator, LayerAnnotatorProducer

logger = get_logger(__name__)


_LAYERS_SYSTEM_PROMPT = """\
Generate layered disclosure views (l0 summary + l1 overview) for each content item below.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- For each item, output its "id" (the numeric index from the [ID: N] marker) plus "l0" and "l1".
- "l0": a 50-100 character summary/abstract of the content, for tight-budget context injection.
- "l1": a 200-500 character overview of key points, for context augmentation.
- Same language as the content. Faithful to the content — do not add new information.
- If the content is too short to meaningfully layer, you may output shorter l0/l1.

Output schema:
[{
  "id": 0,
  "l0": "50-100 char summary",
  "l1": "200-500 char overview"
}]
"""

# L0/L1 分层生成的子批大小：任务单一（仅生成摘要+要点），比主抽取 batch 可以稍大。
_LAYERS_BATCH_SIZE = 16


def _parse_layer_text(raw) -> str:
    """解析 LLM 输出的 l0/l1 文本：strip 后返回；非 str / 空白 → 空串。"""
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _parse_json_array(text: str) -> list[dict] | None:
    """多级兜底解析 JSON 数组：直接解析 → 剥 fence → 正则提取。

    返回 None 表示完全无法解析；否则返回 list[dict]（单对象自动包装）。
    """
    # Level 1: 直接解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    # Level 2: 剥 markdown fence 再解析
    s = text.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    # Level 3: 正则提取 JSON 数组 / 对象
    arr_match = re.search(r"\[.*\]", text, re.DOTALL)
    if arr_match:
        try:
            parsed = json.loads(arr_match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group())
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            pass
    return None


class LLMLayerAnnotator(LayerAnnotator):
    """LLM 版分层标注：对超阈 content 批量调 LLM 生成 L0/L1。"""

    def __init__(
        self,
        llm: LLM,
        *,
        layers_threshold: int = 512,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        super().__init__(layers_threshold=layers_threshold)
        self._llm = llm
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms

    def health(self) -> None:
        try:
            self._llm.health()
        except Exception as exc:
            from common.errors import HealthCheckError
            raise HealthCheckError(str(exc)) from exc

    def annotate(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        logger.info("LLMLayerAnnotator: received %d units", len(units))
        # 仅超阈 content 的 unit 进 LLM 批次
        long_units = [u for u in units if self._should_annotate(u)]
        if not long_units:
            logger.info("LLMLayerAnnotator: no long content (> %d), skip", self._layers_threshold)
            return units

        # 按 _LAYERS_BATCH_SIZE 分批，逐批调 LLM
        for start in range(0, len(long_units), _LAYERS_BATCH_SIZE):
            batch = long_units[start:start + _LAYERS_BATCH_SIZE]
            try:
                self._annotate_batch(batch)
            except Exception:
                logger.warning(
                    "LLMLayerAnnotator: batch failed offset=%d size=%d, skipping",
                    start, len(batch),
                )

        filled = sum(1 for u in long_units if u.layers.l0 or u.layers.l1)
        logger.info(
            "LLMLayerAnnotator: layers generated for %d/%d long units (total %d)",
            filled, len(long_units), len(units),
        )
        return units

    def _annotate_batch(self, units: List[MemoryUnit]) -> None:
        """单批 L0/L1 生成：拼 prompt → 调 LLM → 解析 JSON → 回填 unit.layers。"""
        from common.type_def import ChatMessage

        # 每条 unit 用数字索引标记 [ID: N]，LLM 在输出里用 "id": N 回指
        items = [
            f"---\n[ID: {i}]\n{u.content}\n---"
            for i, u in enumerate(units)
        ]
        user_text = "\n".join(items)
        messages = [
            ChatMessage(role="system", content=_LAYERS_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_text),
        ]

        # 按候选数估算所需输出 token
        layers_max_tokens = max(4096, len(units) * 1200)
        response = self._call_llm_with_retry(messages, max_tokens=layers_max_tokens)

        parsed = _parse_json_array(response)
        if parsed is None:
            logger.warning(
                "LLMLayerAnnotator: response not valid JSON (len=%d), raw: %s",
                len(response), response[:500],
            )
            return

        filled = 0
        for item in parsed:
            try:
                idx = int(item.get("id", -1))
            except (ValueError, TypeError):
                continue
            if 0 <= idx < len(units):
                units[idx].layers.l0 = _parse_layer_text(item.get("l0"))
                units[idx].layers.l1 = _parse_layer_text(item.get("l1"))
                filled += 1
        logger.info(
            "LLMLayerAnnotator: batch filled %d/%d units", filled, len(units),
        )

    def _call_llm_with_retry(self, messages: list, max_tokens: int = 4096) -> str:
        """调用 LLM.chat()，含重试逻辑。"""
        import time

        last_exc = None
        for attempt in range(self._retry_max_retries):
            try:
                return self._llm.chat(messages, temperature=0, max_tokens=max_tokens)
            except Exception as exc:
                last_exc = exc
                if attempt < self._retry_max_retries - 1:
                    wait = self._retry_backoff_ms * (2**attempt) / 1000.0
                    logger.warning(
                        "LLMLayerAnnotator: LLM call failed (attempt %d), retrying in %.1fs",
                        attempt + 1, wait,
                    )
                    time.sleep(wait)
        if last_exc is None:
            raise RuntimeError("LLM 调用未执行：retry_max_retries 必须 >= 1")
        raise last_exc


# -- 注册到 LayerAnnotatorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@LayerAnnotatorProducer.register("llm")
def _build(config):
    return LLMLayerAnnotator(
        llm=LlmProducer.dep(config, default="echo"),
        layers_threshold=config.get("layer_annotator_threshold", 512),
        retry_max_retries=config.get("layer_annotator_retry_max", 3),
        retry_backoff_ms=config.get("layer_annotator_retry_backoff", 1000),
    )
