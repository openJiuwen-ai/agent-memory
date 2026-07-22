# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
ForgetEvaluator abstraction + built-in Ebbinghaus implementation.

Design contract:

    - ``ForgetEvaluator`` is an ABC with one method: ``evaluate``.
      Implementations decide how to score / filter candidate memories
      and return the subset that should be marked as forgotten.
    - ``ForgetContext`` is a plain dataclass carrying per-invocation
      state (``scope_id`` / ``user_id`` / ``now`` / config).
      Cross-cutting dependencies (``kv_store`` / SQL engines / external
      API clients) are NOT part of the context — they go through
      subclass ``__init__`` so the context field set stays stable as
      new data sources are added.
    - The built-in ``EbbinghausEvaluator`` scores memories with:
          score = min(1, salience · temporalDecay + reinforcementBoost)
      and applies the hard filters (``is_important`` skip +
      ``min_retention_days`` skip) before scoring.

DEVIATION FROM SPEC:
    The original spec declares ``evaluate`` as a *sync* method that
    reads ``retrieve_history`` directly from ``self.kv_store.get(key)``.
    That signature does not work in practice — every concrete
    ``BaseKVStore.get`` / ``BaseKVStore.mget`` in this codebase is
    declared ``async``. The dreaming orchestrator is itself async, so
    we promote ``evaluate`` to ``async`` to keep the contract
    implementable without forcing evaluators into fragile
    sync-peek-into-async tricks. This is an additive change for
    downstream custom evaluators — they just need to ``await`` inside
    their ``evaluate`` body. The orchestrator will
    ``await evaluator.evaluate(ctx, memories)`` accordingly.

The module also exposes the ``parse_iso`` helper used internally by
the Ebbinghaus scorer (``_score`` parses ``MemoryDoc.timestamp`` and
the retrieve_history list, ``evaluate`` parses
``latest_retrieve_time``). Custom evaluators that want to reuse the
same lenient timestamp parsing can import it from here.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
    from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
    from jiuwen_memory.memory_core.config.config import ForgettingConfig


# --- formula constants -----------------------------------------------------

# per-mem-type base weight; types not listed fall back to 0.5 (the
# ``summary`` weight) — they are the lowest-priority candidates by default.
TYPE_WEIGHT: dict[str, float] = {
    "user_profile": 1.0,
    "episodic_memory": 0.8,
    "semantic_memory": 0.7,
    "summary": 0.5,
}

# reinforcement weight applied to Σ_i 1/daysSinceAccess_i
SIGMA: float = 0.3


# --- free helpers ------------------------------------------------------------


def parse_iso(s: str | datetime | None) -> datetime:
    """
    Lenient ISO 8601 / datetime parser.

    Accepts ``datetime`` (returned as-is, with naive datetimes promoted
    to local-aware), ISO 8601 strings (with or without a trailing
    ``Z``), and ``None`` / empty strings (returns aware ``datetime.now()``).

    All return values are timezone-aware (local time). The rest of the
    codebase writes aware timestamps (``datetime.now(timezone.utc).
    astimezone()``), so parse_iso's contract is "always return aware,
    promote naive to local". This keeps subtraction against
    ``ForgetContext.now`` (aware) from raising
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``.

    The ``Z`` suffix is normalized to ``+00:00`` because the stdlib
    parser only accepts the explicit offset form. Naive datetimes
    parsed from strings (e.g. ``2026-07-21T21:53:21.835294``) are
    assumed to be in the local timezone and stamped with the local
    offset to match.
    """
    if s is None:
        return datetime.now(tz=timezone.utc).astimezone()
    if isinstance(s, datetime):
        if s.tzinfo is not None:
            return s.astimezone()
        return s.astimezone()
    if not s:
        return datetime.now(tz=timezone.utc).astimezone()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # last-resort fallback: don't let a malformed timestamp blow up
        # the whole forget sweep; treat it as "now" so the memory
        # effectively becomes non-evictable this round.
        return datetime.now(tz=timezone.utc).astimezone()
    if dt.tzinfo is not None:
        return dt.astimezone()
    # Naive ISO string → assume local timezone (matches the rest of
    # the codebase's aware-now convention).
    return dt.astimezone()


# --- ForgetContext -----------------------------------------------------------


@dataclass
class ForgetContext:
    """
    Per-invocation state for one forget sweep.

    Holds only the things that change call-to-call (scope / user /
    timestamp / config). Cross-cutting dependencies live on the
    ``ForgetEvaluator`` subclass instance, not here — see the module
    docstring for the rationale.
    """

    scope_id: str
    user_id: str
    now: datetime
    forgetting_config: "ForgettingConfig"


# --- ForgetEvaluator ABC ----------------------------------------------------


class ForgetEvaluator(ABC):
    """
    Forget-step policy abstraction.

    Implementations receive the candidate list (already filtered to the
    configurable ``mem_type`` whitelist) plus a ``ForgetContext`` and
    return the subset that should be marked ``blacklisted=True`` in
    this round. The orchestrator handles the actual write.

    The contract is deliberately tiny: a single ``evaluate`` method.
    This is intentional — the orchestrator must stay oblivious to the
    scoring formula (Ebbinghaus vs LRU vs business-field expiry) so new
    policies can plug in without touching the dreaming pipeline.
    """

    @abstractmethod
    async def evaluate(
        self,
        ctx: ForgetContext,
        memories: list["MemoryDoc"],
    ) -> list["MemoryDoc"]:
        """
        Args:
            ctx: per-invocation state (scope / user / now / config).
            memories: candidate memories already filtered to the
                configurable ``mem_type`` whitelist.

        Returns:
            The subset to be marked forgotten this round. The order
            is not load-bearing — the orchestrator applies blacklist
            updates to all of them.
        """
        ...


# --- built-in EbbinghausEvaluator -------------------------------------------


class EbbinghausEvaluator(ForgetEvaluator):
    """
    Built-in default evaluator implementing the Ebbinghaus decay formula.

    score = min(1, salience · temporalDecay + reinforcementBoost)

        salience           = type_weight + min(0.2, retrieve_count · 0.02)
        temporalDecay      = exp(-Δt / baseS)         Δt = now - created_at (days)
        reinforcementBoost = SIGMA · Σ_i 1 / daysSinceAccess_i

    Hard filters applied BEFORE scoring (i.e. these memories never
    reach the score computation):

        1. ``MemoryDoc.is_important`` — protected from forgetting.
        2. memories whose latest retrieve time is within
           ``min_retention_days`` of ``now``.

    After scoring, memories with ``score < threshold`` are sorted by
    ascending score and truncated to ``max_evict`` to prevent
    avalanche eviction.
    """

    def __init__(self, kv_store: "BaseKVStore", base_s: int = 30):
        """
        Args:
            kv_store: the same KV store ``LongTermMemory`` uses for
                user/session variables. retrieve_history is co-located
                there so this evaluator can read it without taking a
                dependency on any other storage backend.
            base_s: Ebbinghaus time constant in days (``exp(-Δt / base_s)``).
                Larger → slower decay. Must be ≥ 1.
        """
        if base_s < 1:
            raise ValueError(f"base_s must be >= 1, got {base_s}")
        self.kv_store = kv_store
        self.base_s = base_s

    async def evaluate(
        self,
        ctx: ForgetContext,
        memories: list["MemoryDoc"],
    ) -> list["MemoryDoc"]:
        if not memories:
            return []

        # 1. batch-load retrieve_history once (avoid N KV round-trips
        #    for N candidate memories).
        rh_map = await self._batch_load_retrieve_history(ctx, [m.id for m in memories])

        # 2-3. pre-filter is_important + min_retention_days
        candidates: list[tuple["MemoryDoc", dict]] = []
        for doc in memories:
            if doc.is_important:
                continue
            rh = rh_map.get(doc.id, {})
            latest = rh.get("latest_retrieve_time")
            if latest is not None:
                latest_dt = parse_iso(latest)
                if (ctx.now - latest_dt).days <= ctx.forgetting_config.min_retention_days:
                    continue
            candidates.append((doc, rh))

        # 4. score remaining candidates
        scored = [(doc, self._score(ctx, doc, rh)) for doc, rh in candidates]

        # 5. threshold + sort + truncate
        threshold = ctx.forgetting_config.threshold
        max_evict = ctx.forgetting_config.max_evict
        evictable = [(doc, s) for doc, s in scored if s < threshold]
        evictable.sort(key=lambda ds: ds[1])  # ascending — weakest first
        return [doc for doc, _ in evictable[:max_evict]]

    # -- helpers ------------------------------------------------------------

    async def _batch_load_retrieve_history(
        self,
        ctx: ForgetContext,
        mem_ids: list[str],
    ) -> dict[str, dict]:
        """
        Fetch retrieve_history for each mem_id in one pass via ``mget``.

        Uses ``BaseKVStore.mget`` to amortize the round-trip across N
        candidate memories; falls back to per-key ``get`` for stores
        that don't implement ``mget`` or raise. Always returns a
        ``{mem_id: dict | {}}`` map — never propagates KV errors to the
        scorer (a missing / corrupt history is treated as "no
        reinforcement", which is the safe default — no eviction purely
        due to KV read failure).
        """
        if not mem_ids:
            return {}
        keys = [self._rh_key(ctx, mid) for mid in mem_ids]

        # Try the batch path first.
        raw_values: list[Optional[str | bytes]] = [None] * len(keys)
        try:
            mget = getattr(self.kv_store, "mget", None)
            if callable(mget):
                raw_values = list(await mget(keys))
        except Exception:
            raw_values = [None] * len(keys)

        # Fill any None slots via per-key get (covers backends without mget
        # and key-level failures inside mget).
        for idx, key in enumerate(keys):
            if raw_values[idx] is None:
                try:
                    raw_values[idx] = await self.kv_store.get(key)
                except Exception:
                    raw_values[idx] = None

        result: dict[str, dict] = {}
        for idx, mid in enumerate(mem_ids):
            raw = raw_values[idx]
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8", errors="ignore")
                except Exception:
                    raw = None
            try:
                result[mid] = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                result[mid] = {}
        return result

    @staticmethod
    def _rh_key(ctx: ForgetContext, mem_id: str) -> str:
        """
        retrieve_history KV key — co-located with user/session vars.

        Layout: ``retrieve_history{SEP}{user_id}{SEP}{scope_id}{SEP}{mem_id}``
        where ``SEP`` is ``VariableManager.SEPARATOR`` (currently ``/``).
        The leading ``retrieve_history`` prefix is registered in
        ``kv_prefix_registry`` (Step 4) so it gets first-class treatment
        in scans / migrations / batch cleanup.
        """
        # late import to avoid pulling the manage layer into foundation
        # at module load time (foundation must not depend on manage).
        from jiuwen_memory.memory_core.manage.index.variable_manager import VariableManager

        sep = VariableManager.SEPARATOR
        return f"retrieve_history{sep}{ctx.user_id}{sep}{ctx.scope_id}{sep}{mem_id}"

    def _score(
        self,
        ctx: ForgetContext,
        doc: "MemoryDoc",
        rh: dict,
    ) -> float:
        """
        Ebbinghaus decay formula — see module docstring.

        Returns a float in [0, 1]. A score below ``threshold`` (default
        0.15) qualifies the memory for eviction.
        """
        retrieve_count = rh.get("retrieve_count", 0) or 0
        retrieve_history = rh.get("retrieve_history", []) or []
        type_weight = TYPE_WEIGHT.get(doc.type, 0.5)
        salience = type_weight + min(0.2, retrieve_count * 0.02)

        created_at = parse_iso(doc.timestamp)
        delta_days = max(0, (ctx.now - created_at).days)
        temporal_decay = math.exp(-delta_days / self.base_s)

        reinforcement = 0.0
        for ts in retrieve_history:
            d = max(1, (ctx.now - parse_iso(ts)).days)
            reinforcement += 1.0 / d
        reinforcement *= SIGMA

        return min(1.0, salience * temporal_decay + reinforcement)
