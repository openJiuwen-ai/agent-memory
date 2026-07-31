"""LLMLayerAnnotator — LLM 版分层标注（background / infer=true 路径用）。

对超阈 content 的 unit 批量调 LLM 生成 L0/L1，回填 ``unit.layers``。任务单一
（仅生成摘要+要点）、prompt 精简，避免 L0/L1 指令混入主抽取 prompt 导致输出超限。

仅对 ``len(content) > layers_threshold`` 的 unit 标注，短 content 留空。LLM 批量输出先做
完整性校验，再一次性回填；失败时逐条重试，仍失败则生成严格短于 L2 的确定性提取式层，
避免错位摘要污染记忆或让 L1 与 L2 等长。
"""

from __future__ import annotations

import json
import re

from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import MemoryUnit
from construction.layer_annotator import LayerAnnotator, LayerAnnotatorProducer

logger = get_logger(__name__)


_LAYERS_SYSTEM_PROMPT = """\
Generate layered disclosure views (l0 summary + l1 overview) for each content item below.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- For each item, output its "id" (the numeric index from the [ID: N] marker) plus "l0" and "l1".
- Return every input id exactly once. Never duplicate, omit, reorder the meaning of,
  or invent an id.
- Compress in this strict order: L2 input -> L1 detailed condensation -> L0 compact summary.
- Length must be strictly monotonic for every item: 0 < len(l0) < len(l1) < len(L2).
- "l1": normally 200-500 characters, but always shorter than L2. Preserve every distinct entity,
  action, object, number, date, state, negation, pending item, and old/new value needed to answer
  factual questions. Do not merge separate actions into one generic event.
- "l0": normally 50-100 characters, but always shorter than L1. Keep the central entity, action,
  object, and state; omit supporting detail before dropping the central relation.
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

_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]*")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_EN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}


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


def _language_family(text: str) -> str | None:
    """粗粒度识别中英文；代码、标识符等不明显文本返回 None。"""
    cjk_count = len(_CJK_RE.findall(text))
    latin_count = sum(char.isascii() and char.isalpha() for char in text)
    # 中英文技术文本常含较长产品名；只要中文仍占可观比例，就按中文处理。
    if cjk_count >= 4 and cjk_count * 4 >= latin_count:
        return "cjk"
    if latin_count >= 12 and latin_count > cjk_count * 2:
        return "latin"
    return None


def _semantic_anchors(text: str) -> set[str]:
    """提取用于防串位的轻量词法锚点，不承担语义相似度判断。"""
    anchors = {
        token.lower()
        for token in _WORD_RE.findall(text)
        if len(token) >= 3 and token.lower() not in _EN_STOPWORDS
    }
    for run in _CJK_RUN_RE.findall(text):
        anchors.update(run[index : index + 2] for index in range(len(run) - 1))
    return anchors


def _validate_layer_text(source: str, layer: str, *, field: str) -> None:
    if not layer:
        raise ValueError(f"{field} is empty")
    source_language = _language_family(source)
    layer_language = _language_family(layer)
    if source_language and layer_language and source_language != layer_language:
        raise ValueError(
            f"{field} language mismatch: source={source_language}, layer={layer_language}"
        )
    if not (_semantic_anchors(source) & _semantic_anchors(layer)):
        raise ValueError(f"{field} has no lexical anchor from source")


def _validate_monotonic_layers(source: str, l0: str, l1: str) -> None:
    if not 0 < len(l0) < len(l1) < len(source):
        raise ValueError(
            "layer lengths are not strictly monotonic: "
            f"l0={len(l0)}, l1={len(l1)}, l2={len(source)}"
        )


def _clip_layer(text: str, limit: int) -> str:
    """Take a readable extractive prefix no longer than ``limit``."""
    if len(text) <= limit:
        return text
    prefix = text[:limit].rstrip()
    boundary = max(prefix.rfind(". "), prefix.rfind("。"), prefix.rfind("; "))
    if boundary >= max(1, limit // 2):
        prefix = prefix[: boundary + 1].rstrip()
    return prefix


def _fallback_layers(unit: MemoryUnit) -> tuple[str, str]:
    """Deterministic extractive fallback with strict L0 < L1 < L2."""
    content = unit.content.strip()
    if len(content) < 3:
        return "", ""
    l1_limit = min(500, max(2, int(len(content) * 0.6)))
    l1_limit = min(l1_limit, len(content) - 1)
    l1 = _clip_layer(content, l1_limit)
    if len(l1) >= len(content):
        l1 = content[: len(content) - 1].rstrip()
    l0_limit = min(100, max(1, int(len(l1) * 0.35)))
    l0_limit = min(l0_limit, len(l1) - 1)
    l0 = _clip_layer(l1, l0_limit)
    if not 0 < len(l0) < len(l1) < len(content):
        l1 = content[: max(2, len(content) - 1)].rstrip()
        l0 = l1[: max(1, len(l1) - 1)].rstrip()
    return l0, l1


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

    def annotate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        logger.info("LLMLayerAnnotator: received %d units", len(units))
        # 仅超阈 content 的 unit 进 LLM 批次
        long_units = [u for u in units if self._should_annotate(u)]
        if not long_units:
            logger.info("LLMLayerAnnotator: no long content (> %d), skip", self._layers_threshold)
            return units

        # 按 _LAYERS_BATCH_SIZE 分批，逐批调 LLM
        for start in range(0, len(long_units), _LAYERS_BATCH_SIZE):
            batch = long_units[start : start + _LAYERS_BATCH_SIZE]
            try:
                self._annotate_batch(batch)
            except Exception as exc:
                logger.warning(
                    "LLMLayerAnnotator: batch failed offset=%d size=%d (%s); "
                    "retrying items individually",
                    start,
                    len(batch),
                    exc,
                )
                for unit in batch:
                    try:
                        self._annotate_batch([unit])
                    except Exception as item_exc:
                        l0, l1 = _fallback_layers(unit)
                        unit.layers.l0 = l0
                        unit.layers.l1 = l1
                        logger.warning(
                            "LLMLayerAnnotator: item %s failed (%s); using source-bound fallback",
                            unit.id,
                            item_exc,
                        )

        filled = sum(1 for u in long_units if u.layers.l0 or u.layers.l1)
        logger.info(
            "LLMLayerAnnotator: layers generated for %d/%d long units (total %d)",
            filled,
            len(long_units),
            len(units),
        )
        return units

    def _annotate_batch(self, units: list[MemoryUnit]) -> None:
        """生成并原子回填一批 L0/L1；任何完整性问题都会使整批失败。"""
        from common.type_def import ChatMessage

        # 每条 unit 用数字索引标记 [ID: N]，LLM 在输出里用 "id": N 回指
        items = [f"---\n[ID: {i}]\n{u.content}\n---" for i, u in enumerate(units)]
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
            raise ValueError(f"response not valid JSON (len={len(response)}): {response[:200]}")

        staged: dict[int, tuple[str, str]] = {}
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("layer output item is not an object")
            raw_id = item.get("id", -1)
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                raise ValueError("layer output id is not an integer")
            idx = raw_id
            if not 0 <= idx < len(units):
                raise ValueError(f"layer output id {idx} is outside batch")
            if idx in staged:
                raise ValueError(f"duplicate layer output id {idx}")

            l0 = _parse_layer_text(item.get("l0"))
            l1 = _parse_layer_text(item.get("l1"))
            staged[idx] = (l0, l1)

        expected_ids = set(range(len(units)))
        if set(staged) != expected_ids:
            missing = sorted(expected_ids - set(staged))
            raise ValueError(f"layer output does not cover batch; missing ids={missing}")

        # ID 结构校验全部通过后才写入，防止错位污染。单条文本/长度
        # 异常只跳过该条，不抹掉同批其他合法分层，也不触发整批重试。
        filled = 0
        for idx, (l0, l1) in staged.items():
            try:
                _validate_layer_text(units[idx].content, l0, field="l0")
                _validate_layer_text(units[idx].content, l1, field="l1")
                _validate_monotonic_layers(units[idx].content, l0, l1)
            except ValueError as exc:
                logger.warning(
                    "LLMLayerAnnotator: skipping invalid item id=%d unit=%s: %s",
                    idx,
                    units[idx].id,
                    exc,
                )
                continue
            units[idx].layers.l0 = l0
            units[idx].layers.l1 = l1
            filled += 1
        logger.info(
            "LLMLayerAnnotator: batch filled %d/%d unique units",
            filled,
            len(units),
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
                        attempt + 1,
                        wait,
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
