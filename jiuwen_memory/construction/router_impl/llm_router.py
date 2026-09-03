"""LLMRouter — 归属判定的模型实现（F07「归属判定算子」）。

对一批候选单元发一次 LLM 调用，逐条产出「命中哪个归属类别」与「哪些收窄维为真」两项
判断，其余（落点解析、记录维标签、fallback 回落、两个落盘不变量）由
:mod:`construction.router` 的公共函数完成——放实现内则换一个实现即可能漏掉。

每批一次调用，不逐条调用：判定的输入是同一次交互产生的一批派生记忆，逐条调用既放大时延
又使同批条目的判据不一致。

LLM 不可用或输出无法解析时抛出，由调用处（:func:`~construction.router.route_batch`）统一
按「全批落 fallback、不阻断写入」处置。判定失败的降级不在本实现内部各写一份。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, List

from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger, metadata_for_log
from jiuwen_memory.common.type_def import ChatMessage, MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.router import (
    RouteContext,
    RouteDecision,
    Router,
    RouterProducer,
    RouteTable,
    build_decision,
    parse_route_table,
)

logger = get_logger(__name__)

# 单批 LLM 调用的条数上限，超批分多次（与 LLMClassifier 同口径）。
_ROUTE_BATCH_SIZE = 10

_SYSTEM_PROMPT = """\
You assign each memory below to exactly one ownership class, and answer one yes/no question \
per narrowing dimension.

Output ONLY a JSON array. No explanation, no markdown fences. One entry per input memory, in \
the SAME order as input. Each entry:

- "source_id": the bare id of the input memory (the UUID shown in the [ID: ...] marker).
- "memory_class": the name of exactly one class from the CLASSES list below. Pick the class \
whose ownership entity the memory would stop being true for if you swapped it out. When no \
class clearly fits, use the class marked FALLBACK.
- "narrow": an object mapping each narrowing dimension key to true or false, answering that \
dimension's question for this memory. Omitted keys are treated as false.
- "discard": true only when the memory carries no reusable information at all. Default false.

CLASSES:
{classes}

NARROWING DIMENSIONS:
{dims}

Output schema:
[{{"source_id": "...", "memory_class": "...", "narrow": {{"...": true}}, "discard": false}}]
"""

_SOURCE_PREFIX = "[ID: {unit_id}]\n{unit_content}\n"

# LLM 有时把 id 包在 [ID: ...] 里回传，去壳后再比对。
_ID_SHELL = re.compile(r"^\[?ID:\s*|\]$")


def _strip_source_id_shell(raw: str) -> str:
    return _ID_SHELL.sub("", raw.strip()).strip()


class LLMRouter(Router):
    """模型实现：一次调用判一批的归属类别与收窄维取值。"""

    def __init__(
        self,
        llm: LLM,
        table: RouteTable,
        *,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._table = table
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms

    @property
    def table(self) -> RouteTable:
        return self._table

    def operator_type(self) -> OperatorType:
        return OperatorType.ROUTER

    def health(self) -> None:
        try:
            self._llm.health()
        except Exception as exc:
            from jiuwen_memory.common.errors import HealthCheckError

            raise HealthCheckError(str(exc)) from exc

    def route(self, units: List[MemoryUnit], ctx: RouteContext) -> List[RouteDecision]:
        if not units:
            return []
        decisions: list[RouteDecision] = []
        for start in range(0, len(units), _ROUTE_BATCH_SIZE):
            end = start + _ROUTE_BATCH_SIZE
            sub = units[start:end]
            decisions.extend(self._route_batch(sub, ctx))
        return decisions

    def _route_batch(
        self, units: List[MemoryUnit], ctx: RouteContext
    ) -> List[RouteDecision]:
        messages = [
            ChatMessage(role="system", content=self._system_prompt(ctx)),
            ChatMessage(
                role="user",
                content="\n".join(
                    _SOURCE_PREFIX.format(unit_id=unit.id, unit_content=unit.content)
                    for unit in units
                ),
            ),
        ]
        raw_items = _parse_response(self._call_llm_with_retry(messages))
        items = [item for item in raw_items if isinstance(item, dict)]
        by_id: dict[str, dict[str, Any]] = {
            _strip_source_id_shell(str(item.get("source_id", ""))): item for item in items
        }
        # id 缺失或重复时按 id 比对必然错位：空串键互相覆盖，整批取到同一条结论，且失效
        # 方向是静默错落点。此时退回按输入顺序取，并记 WARNING——调用方应给唯一 id。
        by_position = self._ids_unusable(units)
        if by_position:
            logger.warning(
                "LLMRouter: %d 条输入的 id 缺失或重复，改按输入顺序比对判定结论", len(units)
            )

        results: list[RouteDecision] = []
        for index, unit in enumerate(units):
            if by_position:
                item = items[index] if index < len(items) else None
            else:
                item = by_id.get(unit.id)
            if item is None:
                # 单条缺判：落 fallback，不牵连同批其余条目。
                results.append(build_decision(unit, "", (), ctx, reason="no decision for unit"))
                continue
            narrow = item.get("narrow")
            hits = (
                tuple(key for key, value in narrow.items() if bool(value))
                if isinstance(narrow, dict)
                else ()
            )
            decision = build_decision(
                unit,
                str(item.get("memory_class", "") or ""),
                hits,
                ctx,
                discarded=bool(item.get("discard")),
                reason=str(item.get("reason", "") or ""),
            )
            routing_metadata = {
                "memory_class": decision.memory_class,
                "space": decision.scope.space,
                "tags": decision.tags,
                "discarded": decision.discarded,
            }
            logger.info(
                "LLMRouter: unit=%s routing_metadata=%s",
                unit.id[:8],
                metadata_for_log(routing_metadata),
            )
            results.append(decision)
        return results

    @staticmethod
    def _ids_unusable(units: List[MemoryUnit]) -> bool:
        """id 是否不足以比对：任一条为空，或存在重复。"""
        ids = [unit.id for unit in units]
        return not all(ids) or len(set(ids)) != len(ids)

    def _system_prompt(self, ctx: RouteContext) -> str:
        classes = "\n".join(
            "- {name}{fallback}: {description}".format(
                name=item.name,
                fallback=" (FALLBACK)" if item.fallback else "",
                description=item.description or "no description given",
            )
            for item in ctx.classes
        )
        dims = "\n".join(
            "- {key}: {question}".format(
                key=dim.tag_key,
                question=dim.question or f"is this memory specific to one {dim.entity}?",
            )
            for dim in ctx.narrow_dims
        ) or "- (none)"
        return _SYSTEM_PROMPT.format(classes=classes, dims=dims)

    def _call_llm_with_retry(self, messages: list) -> str:
        last_exc: Exception | None = None
        for attempt in range(self._retry_max_retries):
            try:
                return self._llm.chat(messages, temperature=0, max_tokens=4096)
            except Exception as exc:  # noqa: BLE001 —— 重试后仍失败即上抛
                last_exc = exc
                if attempt < self._retry_max_retries - 1:
                    wait = self._retry_backoff_ms * (2**attempt) / 1000.0
                    logger.warning(
                        "LLMRouter: LLM call failed (attempt %d), retrying in %.1fs",
                        attempt + 1,
                        wait,
                    )
                    time.sleep(wait)
        if last_exc is None:
            raise RuntimeError("LLMRouter: LLM 调用未执行（retry_max_retries 必须 >= 1）")
        raise last_exc


def _parse_response(response: str) -> list[dict]:
    """解析 LLM 返回的 JSON 数组（容错 markdown fence 与单对象形态）。"""
    text = response.strip()
    if text.startswith("```"):
        text = text.strip("`")
        newline = text.find("\n")
        if newline >= 0:
            text = text[newline + 1:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLMRouter: 无法解析判定输出：{exc}") from exc
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise ValueError(f"LLMRouter: 判定输出应是 JSON 数组，得到 {type(parsed).__name__}")


@RouterProducer.register("llm")
def _build(config):
    return LLMRouter(
        LlmProducer.dep(config, default="echo"),
        parse_route_table(
            {
                "coord_entities": config.get("coord_entities"),
                "memory_classes": config.get("memory_classes"),
                "narrow_dims": config.get("narrow_dims"),
            }
        ),
        retry_max_retries=int(config.get("retry_max_retries", 3)),
        retry_backoff_ms=int(config.get("retry_backoff_ms", 1000)),
    )
