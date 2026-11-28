"""LLMRouter — 归属判定的模型实现（群体记忆 F07「归属判定算子」）。

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
from typing import Any

from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger, metadata_for_log, redact_for_log
from jiuwen_memory.common.type_def import ChatMessage, MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.common import (
    batch_source_table,
    is_ordinal_reference,
    reject_deprecated_llm_attempt_keys,
    strip_batch_reference_shell,
    validate_llm_attempt_policy,
)
from jiuwen_memory.construction.llm_retry import call_llm_with_retry
from jiuwen_memory.construction.prompt_guard import truncate_unit_content
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

OWNERSHIP COORDINATES: {coords}

Output ONLY a JSON array. No explanation, no markdown fences. One entry per input memory, in \
the SAME order as input. Each entry:

- "source_id": the integer shown in the input memory's [ID: ...] marker.
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
[{{"source_id": 1, "memory_class": "...", "narrow": {{"...": true}}, "discard": false}}]
"""

_SOURCE_PREFIX = "[ID: {unit_id}]\n{unit_content}\n"


class LLMRouter(Router):
    """模型实现：一次调用判一批的归属类别与收窄维取值。"""

    def __init__(
        self,
        llm: LLM,
        table: RouteTable,
        *,
        max_attempts: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._table = table
        self._max_attempts, self._retry_backoff_ms = validate_llm_attempt_policy(
            max_attempts, retry_backoff_ms,
        )

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

    def route(self, units: list[MemoryUnit], ctx: RouteContext) -> list[RouteDecision]:
        if not units:
            return []
        decisions: list[RouteDecision] = []
        for start in range(0, len(units), _ROUTE_BATCH_SIZE):
            end = start + _ROUTE_BATCH_SIZE
            sub = units[start:end]
            decisions.extend(self._route_batch(sub, ctx))
        return decisions

    def _route_batch(
        self, units: list[MemoryUnit], ctx: RouteContext
    ) -> list[RouteDecision]:
        references = batch_source_table(units)
        messages = [
            ChatMessage(role="system", content=self._system_prompt(ctx)),
            ChatMessage(
                role="user",
                content="\n".join(
                    _SOURCE_PREFIX.format(
                        unit_id=int(reference),
                        unit_content=truncate_unit_content(unit.content),
                    )
                    for reference, unit in references.items()
                ),
            ),
        ]
        raw_items = _parse_response(self._call_llm_with_retry(messages))
        items = [item for item in raw_items if isinstance(item, dict)]
        by_reference: dict[str, dict[str, Any]] = {}
        for item in items:
            reference = strip_batch_reference_shell(str(item.get("source_id", "")))
            if not is_ordinal_reference(reference):
                logger.warning(
                    "LLMRouter: invalid batch reference %s, skip",
                    redact_for_log(reference),
                )
                continue
            if reference not in by_reference:
                by_reference[reference] = item

        results: list[RouteDecision] = []
        for reference, unit in references.items():
            item = by_reference.get(reference)
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
        coords = ", ".join(f"{k}={v}" for k, v in ctx.coords.items()) or "(none)"
        return _SYSTEM_PROMPT.format(classes=classes, dims=dims, coords=coords)

    def _call_llm_with_retry(self, messages: list) -> str:
        """调用 LLM.chat()——统一走构建层重试入口（超时确定性失败不重试）。"""
        return call_llm_with_retry(
            self._llm.chat,
            messages,
            max_attempts=self._max_attempts,
            backoff_ms=self._retry_backoff_ms,
            logger=logger,
            label="LLMRouter",
            temperature=0,
            max_tokens=4096,
        )


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
    reject_deprecated_llm_attempt_keys(config, "router")
    return LLMRouter(
        LlmProducer.dep(config, default="echo"),
        parse_route_table(
            {
                "coord_entities": config.get("coord_entities"),
                "memory_classes": config.get("memory_classes"),
                "narrow_dims": config.get("narrow_dims"),
            }
        ),
        max_attempts=config.get("router_max_attempts", 3),
        retry_backoff_ms=config.get("router_retry_backoff", 1000),
    )
