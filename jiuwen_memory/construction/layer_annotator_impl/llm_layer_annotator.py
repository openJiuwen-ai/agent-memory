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

from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.layer_annotator import LayerAnnotator, LayerAnnotatorProducer

logger = get_logger(__name__)


_LAYERS_SYSTEM_PROMPT = """\
Generate layered disclosure views (l0 summary + l1 overview) for each content item below.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- For each item, output its "id" (the numeric index from the [ID: N] marker) plus "l0" and "l1".
- Generate L1 from the L2 input, then generate L0 from L1.
- "l0": a compact summary for tight-budget context injection.
- "l1": a more detailed overview for context augmentation.
- Preserve exact entities, actions, objects, numbers, dates, negation, and state.
- Do not merge separate people, actions, records, or events.
- Each result must satisfy 0 < len(l0) < len(l1) < len(L2 input).
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
        """初始化 LLMLayerAnnotator。

        Args:
            llm: 参数 llm（LLM）。
            layers_threshold: 参数 layers_threshold（int）。
            retry_max_retries: 参数 retry_max_retries（int）。
            retry_backoff_ms: 参数 retry_backoff_ms（int）。
        """
        super().__init__(layers_threshold=layers_threshold)
        self._llm = llm
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms

    def health(self) -> None:
        """执行健康检查。

        Raises:
            HealthCheckError: 执行失败时抛出。
        """
        try:
            self._llm.health()
        except Exception as exc:
            from jiuwen_memory.common.errors import HealthCheckError
            raise HealthCheckError(str(exc)) from exc

    def annotate(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """执行 `annotate` 操作。

        Args:
            units: 参数 units（List[MemoryUnit]）。

        Returns:
            返回 List[MemoryUnit]。
        """
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
            except Exception as exc:
                logger.warning(
                    "LLMLayerAnnotator: batch failed offset=%d size=%d, skipping: %s",
                    start, len(batch), exc,
                )

        filled = sum(1 for u in long_units if u.layers.l0 or u.layers.l1)
        logger.info(
            "LLMLayerAnnotator: layers generated for %d/%d long units (total %d)",
            filled, len(long_units), len(units),
        )
        return units

    def _annotate_batch(self, units: List[MemoryUnit]) -> None:
        """单批 L0/L1 生成：拼 prompt → 调 LLM → 解析 JSON → 回填 unit.layers。"""
        from jiuwen_memory.common.type_def import ChatMessage

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
            raise ValueError("layer response is not valid JSON")

        staged: dict[int, tuple[str, str]] = {}
        seen_ids: set[int] = set()
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("layer output item is not an object")
            raw_id = item.get("id")
            if isinstance(raw_id, bool):
                raise ValueError("layer output id is invalid")
            if isinstance(raw_id, int):
                idx = raw_id
            elif isinstance(raw_id, str) and re.fullmatch(r"\d+", raw_id.strip()):
                idx = int(raw_id)
            else:
                raise ValueError("layer output id is invalid")
            if not 0 <= idx < len(units):
                raise ValueError(f"layer output id {idx} is out of range")
            if idx in seen_ids:
                raise ValueError(f"duplicate layer output id {idx}")
            seen_ids.add(idx)
            l0 = _parse_layer_text(item.get("l0"))
            l1 = _parse_layer_text(item.get("l1"))
            if not 0 < len(l0) < len(l1) < len(units[idx].content):
                logger.warning(
                    "LLMLayerAnnotator: invalid layer lengths for id %d, skipping item",
                    idx,
                )
                continue
            staged[idx] = (l0, l1)

        expected = set(range(len(units)))
        if seen_ids != expected:
            raise ValueError(f"layer output ids must exactly match {sorted(expected)}")

        for idx, (l0, l1) in staged.items():
            units[idx].layers.l0 = l0
            units[idx].layers.l1 = l1
        logger.info(
            "LLMLayerAnnotator: batch filled %d/%d units", len(staged), len(units),
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
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return LLMLayerAnnotator(
        llm=LlmProducer.dep(config, default="echo"),
        layers_threshold=config.get("layer_annotator_threshold", 512),
        retry_max_retries=config.get("layer_annotator_retry_max", 3),
        retry_backoff_ms=config.get("layer_annotator_retry_backoff", 1000),
    )
