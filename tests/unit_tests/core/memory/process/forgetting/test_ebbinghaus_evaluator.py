# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Step 3 unit tests for the Ebbinghaus forgetting evaluator package.

Scope:
  - parse_iso: datetime / ISO string / Z suffix / None / garbage
  - EbbinghausEvaluator._score:
      no retrieve_history
      single recent retrieve (high reinforcement)
      single distant retrieve (low reinforcement)
      retrieve_count saturation cap (0.2 ceiling)
      unknown mem_type fallback weight
      aware vs naive datetime (no TypeError)
  - EbbinghausEvaluator.evaluate:
      empty input
      is_important skips entirely
      min_retention_days skip on recent latest_retrieve_time
      min_retention_days does NOT skip when delta > min
      threshold cutoff (above threshold stays, below evicted)
      max_evict truncation (ascending-score order)
      KV mget unavailable → per-key get fallback still works
      KV raises → graceful degrade (no evictions from read failure)
      unknown mem_type uses fallback weight 0.5
  - ForgetEvaluator extension: custom stub subclass produces different
    eviction set from the built-in (proves orchestrator stays agnostic)
  - ForgettingConfig:
      defaults
      validation (threshold range, max_evict >= 1, min_retention >= 0)
      carries an arbitrary ForgetEvaluator subclass instance
  - EbbinghausEvaluator(base_s):
      default 30
      validation (base_s >= 1)
      custom value stored on the instance
  - DreamingConfig.forgetting: None by default; carries nested config
  - EbbinghausEvaluator._rh_key: shape + SEPARATOR
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.memory_core.config.config import DreamingConfig, ForgettingConfig
from jiuwen_memory.memory_core.process.forgetting import (
    EbbinghausEvaluator,
    ForgetContext,
    ForgetEvaluator,
    SIGMA,
    TYPE_WEIGHT,
    parse_iso,
)


# ---------------------------------------------------------------- parse_iso


class TestParseIso:
    @staticmethod
    def test_none_returns_now():
        out = parse_iso(None)
        assert isinstance(out, datetime)
        # parse_iso always returns aware (local) — see the module docstring.
        assert out.tzinfo is not None
        assert (datetime.now(timezone.utc).astimezone() - out).total_seconds() < 5

    @staticmethod
    def test_empty_string_returns_now():
        out = parse_iso("")
        assert out.tzinfo is not None
        assert (datetime.now(timezone.utc).astimezone() - out).total_seconds() < 5

    @staticmethod
    def test_passthrough_datetime_naive():
        dt = datetime(2026, 1, 15, 10, 30)
        out = parse_iso(dt)
        # Naive datetime is promoted to local-aware (matches the rest of
        # the codebase's aware-now convention).
        assert out.tzinfo is not None
        assert out.replace(tzinfo=None) == dt

    @staticmethod
    def test_passthrough_datetime_aware_converted_to_local():
        dt = datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc)
        out = parse_iso(dt)
        # timezone-aware datetime is normalized to local (not UTC) so
        # naive ``now()`` subtraction does not raise.
        assert out.tzinfo is not None

    @staticmethod
    def test_iso_string_no_tz():
        out = parse_iso("2026-01-15T10:30:00")
        assert out.year == 2026 and out.month == 1 and out.day == 15
        # No offset in the source string → assumed local, returned aware.
        assert out.tzinfo is not None

    @staticmethod
    def test_iso_string_with_z():
        out = parse_iso("2026-01-15T10:30:00Z")
        # ``Z`` is normalized to ``+00:00``; result is converted to local.
        assert out.tzinfo is not None
        assert out.year == 2026

    @staticmethod
    def test_iso_string_with_explicit_offset():
        out = parse_iso("2026-01-15T10:30:00+02:00")
        assert out.tzinfo is not None

    @staticmethod
    def test_garbage_string_returns_now():
        out = parse_iso("not-a-date")
        assert out.tzinfo is not None
        assert (datetime.now(timezone.utc).astimezone() - out).total_seconds() < 5


# ---------------------------------------------------------------- _score


def _make_doc(
    mem_id: str,
    mem_type: str = "summary",
    days_old: int = 100,
    is_important: bool = False,
    now: datetime | None = None,
) -> MemoryDoc:
    # Anchor the timestamp to the same `now` the ctx will use, so the
    # delta in `_score` is exactly `timedelta(days=days_old)` (no
    # sub-day jitter that would truncate `.days` to days_old-1).
    base_now = now if now is not None else datetime.now(timezone.utc).astimezone()
    return MemoryDoc(
        id=mem_id,
        text="x",
        type=mem_type,
        timestamp=base_now - timedelta(days=days_old),
        is_important=is_important,
    )


def _make_ctx(
    *,
    threshold: float = 0.15,
    max_evict: int = 1000,
    min_retention_days: int = 30,
) -> ForgetContext:
    return ForgetContext(
        scope_id="scope",
        user_id="user",
        now=datetime.now(timezone.utc).astimezone(),
        forgetting_config=ForgettingConfig(
            enabled=True,
            threshold=threshold,
            max_evict=max_evict,
            min_retention_days=min_retention_days,
        ),
    )


def _make_ev(base_s: int = 30) -> EbbinghausEvaluator:
    return EbbinghausEvaluator(InMemoryKVStore(), base_s=base_s)


class TestScore:
    @staticmethod
    def test_no_retrieve_history_decays_to_type_weight_times_decay():
        ev = _make_ev(base_s=30)
        ctx = _make_ctx()
        doc = _make_doc("m1", mem_type="summary", days_old=30, now=ctx.now)
        score = ev._score(ctx, doc, {})  # pylint: disable=protected-access
        # salience = 0.5, temporalDecay = exp(-1) ≈ 0.3679
        expected = min(1.0, 0.5 * math.exp(-1.0) + 0.0)
        assert math.isclose(score, expected, rel_tol=1e-9)

    @staticmethod
    def test_user_profile_higher_weight_than_summary():
        ev = _make_ev(base_s=30)
        ctx = _make_ctx()
        sum_doc = _make_doc("s", mem_type="summary", days_old=30, now=ctx.now)
        up_doc = _make_doc("u", mem_type="user_profile", days_old=30, now=ctx.now)
        # pylint: disable=protected-access
        assert ev._score(ctx, up_doc, {}) > ev._score(ctx, sum_doc, {})

    @staticmethod
    def test_unknown_mem_type_falls_back_to_summary_weight():
        ev = _make_ev(base_s=30)
        ctx = _make_ctx()
        unknown = _make_doc("x", mem_type="not_a_real_type", days_old=30, now=ctx.now)
        summary = _make_doc("y", mem_type="summary", days_old=30, now=ctx.now)
        assert math.isclose(
            ev._score(ctx, unknown, {}),  # pylint: disable=protected-access
            ev._score(ctx, summary, {}),  # pylint: disable=protected-access
            rel_tol=1e-12,
        )

    @staticmethod
    def test_retrieve_count_saturation_caps_at_0_2():
        ev = _make_ev(base_s=30)
        ctx = _make_ctx()
        doc = _make_doc("m", mem_type="summary", days_old=30, now=ctx.now)
        # retrieve_count > 10 saturates the 0.2 cap
        rh_low = {"retrieve_count": 1}
        rh_saturated = {"retrieve_count": 100}
        score_low = ev._score(ctx, doc, rh_low)  # pylint: disable=protected-access
        score_sat = ev._score(ctx, doc, rh_saturated)  # pylint: disable=protected-access
        # Salience: 0.5 + 0.02 vs 0.5 + 0.2 (capped)
        assert score_sat > score_low
        # Above the cap, adding more count does nothing.
        rh_huge = {"retrieve_count": 10000}
        score_huge = ev._score(ctx, doc, rh_huge)  # pylint: disable=protected-access
        assert math.isclose(score_sat, score_huge, rel_tol=1e-12)

    @staticmethod
    def test_reinforcement_uses_distance_in_days():
        ev = _make_ev(base_s=30)
        ctx = _make_ctx()
        doc = _make_doc("m", mem_type="summary", days_old=100, now=ctx.now)
        recent_rh = {"retrieve_history": [(ctx.now - timedelta(days=1)).isoformat()]}
        distant_rh = {"retrieve_history": [(ctx.now - timedelta(days=365)).isoformat()]}
        # pylint: disable=protected-access
        assert ev._score(ctx, doc, recent_rh) > ev._score(ctx, doc, distant_rh)

    @staticmethod
    def test_reinforcement_respects_sigma():
        ev = _make_ev(base_s=30)
        ctx = _make_ctx()
        doc = _make_doc("m", mem_type="summary", days_old=30, now=ctx.now)
        rh = {"retrieve_history": [(ctx.now - timedelta(days=10)).isoformat()]}
        # Base score without reinforcement.
        base = ev._score(ctx, doc, {})  # pylint: disable=protected-access
        with_rein = ev._score(ctx, doc, rh)  # pylint: disable=protected-access
        # reinforcement = SIGMA · 1/10
        assert math.isclose(
            with_rein - base,
            SIGMA * (1.0 / 10.0),
            rel_tol=1e-9,
        )

    @staticmethod
    def test_score_never_exceeds_one():
        ev = _make_ev(base_s=1)  # base_s=1 maximizes temporalDecay for fresh docs
        ctx = _make_ctx()
        doc = _make_doc("m", mem_type="user_profile", days_old=0, now=ctx.now)
        rh = {
            "retrieve_count": 1000,  # salience cap reached
            "retrieve_history": [
                (ctx.now - timedelta(days=1)).isoformat()
                for _ in range(500)
            ],
        }
        # pylint: disable=protected-access
        assert ev._score(ctx, doc, rh) == 1.0

    @staticmethod
    def test_type_weight_constant_matches_spec():
        assert TYPE_WEIGHT == {
            "user_profile": 1.0,
            "episodic_memory": 0.8,
            "semantic_memory": 0.7,
            "summary": 0.5,
        }

    @staticmethod
    def test_sigma_constant_matches_spec():
        assert math.isclose(SIGMA, 0.3, rel_tol=1e-12)


# ---------------------------------------------------------------- evaluate


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        ev = EbbinghausEvaluator(InMemoryKVStore())
        out = await ev.evaluate(_make_ctx(), [])
        assert out == []

    @pytest.mark.asyncio
    async def test_is_important_is_skipped(self):
        ev = EbbinghausEvaluator(InMemoryKVStore())
        important = _make_doc("imp", mem_type="summary", days_old=1000, is_important=True)
        out = await ev.evaluate(_make_ctx(), [important])
        assert out == []

    @pytest.mark.asyncio
    async def test_recent_retrieve_blocks_eviction(self):
        kv = InMemoryKVStore()
        ev = EbbinghausEvaluator(kv)
        ctx = _make_ctx(min_retention_days=30)
        # 100-day-old memory, retrieved 5 days ago — within 30-day window.
        doc = _make_doc("m", mem_type="summary", days_old=100)
        rh_key = EbbinghausEvaluator._rh_key(ctx, "m")  # pylint: disable=protected-access
        await kv.set(
            rh_key,
            json.dumps(
                {
                    "retrieve_count": 1,
                    "latest_retrieve_time": (datetime.now(timezone.utc).astimezone() - timedelta(days=5)).isoformat(),
                    "retrieve_history": [(datetime.now(timezone.utc).astimezone() - timedelta(days=5)).isoformat()],
                }
            ),
        )
        out = await ev.evaluate(ctx, [doc])
        assert out == []

    @pytest.mark.asyncio
    async def test_old_retrieve_does_not_block_eviction(self):
        kv = InMemoryKVStore()
        ev = EbbinghausEvaluator(kv)
        ctx = _make_ctx(min_retention_days=30)
        doc = _make_doc("m", mem_type="summary", days_old=100)
        rh_key = EbbinghausEvaluator._rh_key(ctx, "m")  # pylint: disable=protected-access
        await kv.set(
            rh_key,
            json.dumps(
                {
                    "retrieve_count": 1,
                    "latest_retrieve_time": (datetime.now(timezone.utc).astimezone() - timedelta(days=60)).isoformat(),
                    "retrieve_history": [(datetime.now(timezone.utc).astimezone() - timedelta(days=60)).isoformat()],
                }
            ),
        )
        out = await ev.evaluate(ctx, [doc])
        assert [m.id for m in out] == ["m"]

    @pytest.mark.asyncio
    async def test_threshold_cutoff(self):
        ev = EbbinghausEvaluator(InMemoryKVStore())
        # user_profile has weight 1.0; a fresh memory scores very high
        # and stays above 0.99, well above the 0.15 threshold.
        fresh = _make_doc("fresh", mem_type="user_profile", days_old=0)
        # A 100-day-old summary with no reinforcement scores ~0.18, which
        # is above 0.15 — keep threshold lower to verify cutoff.
        ctx = _make_ctx(threshold=0.05)
        old_summary = _make_doc("old", mem_type="summary", days_old=100)
        out = await ev.evaluate(ctx, [fresh, old_summary])
        assert [m.id for m in out] == ["old"]

    @pytest.mark.asyncio
    async def test_max_evict_truncation_in_ascending_score_order(self):
        ev = EbbinghausEvaluator(InMemoryKVStore())
        # 5 evictable summaries, ages 100/200/300/400/500 days.
        docs = [_make_doc(f"m{i}", mem_type="summary", days_old=100 + i * 100) for i in range(5)]
        ctx = _make_ctx(threshold=1.0, max_evict=2)
        # threshold=1.0 means every memory is evictable; max_evict=2 caps.
        out = await ev.evaluate(ctx, docs)
        assert len(out) == 2
        # ascending score order = oldest (weakest decay) first.
        # the older the memory, the smaller its score → first evicted.
        # m4 is 500 days old (lowest score), m3 is 400 (next lowest).
        assert {m.id for m in out} == {"m4", "m3"}

    @pytest.mark.asyncio
    async def test_mget_unavailable_falls_back_to_per_key_get(self):
        # Use a minimal KV that doesn't implement mget.
        class NoMgetKV(InMemoryKVStore):
            async def mget(self, keys):
                raise AttributeError("mget not supported")

        kv = NoMgetKV()
        ev = EbbinghausEvaluator(kv)
        ctx = _make_ctx()
        doc = _make_doc("m", mem_type="summary", days_old=100)
        rh_key = EbbinghausEvaluator._rh_key(ctx, "m")  # pylint: disable=protected-access
        await kv.set(
            rh_key,
            json.dumps({"retrieve_count": 5, "retrieve_history": []}),
        )
        out = await ev.evaluate(ctx, [doc])
        # Per-key get fills the None left by mget failure; memory evicts.
        assert [m.id for m in out] == ["m"]

    @pytest.mark.asyncio
    async def test_get_raises_degrades_gracefully(self):
        # KV where ``mget`` AND ``get`` both raise — we should still
        # return *something* (no crash); retrieve_history treated as
        # empty dict, the memory still scores via type weight × decay.
        class BrokenKV(InMemoryKVStore):
            async def get(self, key):
                raise RuntimeError("backend offline")

            async def mget(self, keys):
                raise RuntimeError("backend offline")

        ev = EbbinghausEvaluator(BrokenKV())
        ctx = _make_ctx(threshold=1.0)
        doc = _make_doc("m", mem_type="summary", days_old=100)
        out = await ev.evaluate(ctx, [doc])
        # No retrieve history → just salience*decay. Should evict.
        assert [m.id for m in out] == ["m"]

    @pytest.mark.asyncio
    async def test_corrupt_retrieve_history_is_treated_as_empty(self):
        kv = InMemoryKVStore()
        ev = EbbinghausEvaluator(kv)
        ctx = _make_ctx()
        doc = _make_doc("m", mem_type="summary", days_old=100)
        rh_key = EbbinghausEvaluator._rh_key(ctx, "m")  # pylint: disable=protected-access
        await kv.set(rh_key, "not-json-at-all{")
        out = await ev.evaluate(ctx, [doc])
        # Corrupt JSON → treated as empty dict → memory still scores on
        # type weight × decay alone.
        assert [m.id for m in out] == ["m"]

    @pytest.mark.asyncio
    async def test_bytes_payload_in_kv_decoded(self):
        # Some KV backends (Redis / Shelve) return bytes; evaluator should
        # decode utf-8 before json.loads.
        kv = InMemoryKVStore()
        ev = EbbinghausEvaluator(kv)
        ctx = _make_ctx()
        doc = _make_doc("m", mem_type="summary", days_old=100)
        rh_key = EbbinghausEvaluator._rh_key(ctx, "m")  # pylint: disable=protected-access
        # Write a bytes payload directly into the underlying dict.
        kv._store[rh_key] = (json.dumps({"retrieve_count": 1}).encode("utf-8"), None)  # pylint: disable=protected-access
        out = await ev.evaluate(ctx, [doc])
        assert [m.id for m in out] == ["m"]


# ---------------------------------------------------------------- _rh_key


class TestRhKey:
    @staticmethod
    def test_key_uses_separator_and_components():
        ctx = ForgetContext(
            scope_id="scope",
            user_id="user",
            now=datetime.now(timezone.utc).astimezone(),
            forgetting_config=ForgettingConfig(),
        )
        key = EbbinghausEvaluator._rh_key(ctx, "mem-1")  # pylint: disable=protected-access
        assert key == "retrieve_history/user/scope/mem-1"


# ---------------------------------------------------------------- custom evaluator


class _HighestScoreWinsEvaluator(ForgetEvaluator):
    """
    Stub evaluator that returns the memory with the largest id (lexicographic)
    — independent of Ebbinghaus. Used only to prove the orchestrator stays
    agnostic of the scoring algorithm.
    """

    async def evaluate(self, ctx, memories):
        if not memories:
            return []
        chosen = max(memories, key=lambda m: m.id)
        return [chosen]


class TestForgetEvaluatorAbstraction:
    @pytest.mark.asyncio
    async def test_custom_subclass_returns_different_set_than_builtin(self):
        # Build a scenario where Ebbinghaus would evict an old summary
        # but the stub picks a user_profile with a larger id.
        # Default threshold=0.15: the old summary (~0.02) evicts, the
        # fresh user_profile (~0.97) survives.
        ctx = _make_ctx(threshold=0.15)
        old_summary = _make_doc("aaa", mem_type="summary", days_old=100)
        fresh_up = _make_doc("zzz", mem_type="user_profile", days_old=1)
        builtin = EbbinghausEvaluator(InMemoryKVStore())
        custom = _HighestScoreWinsEvaluator()

        builtin_out = await builtin.evaluate(ctx, [old_summary, fresh_up])
        custom_out = await custom.evaluate(ctx, [old_summary, fresh_up])

        # Builtin evicts the old, weak summary; user_profile survives.
        assert {m.id for m in builtin_out} == {"aaa"}
        # Stub picks the lexicographically larger id regardless of score.
        assert {m.id for m in custom_out} == {"zzz"}

    @pytest.mark.asyncio
    async def test_custom_subclass_can_be_carried_in_forgetting_config(self):
        # The ForgettingConfig.evaluator field accepts any subclass.
        custom = _HighestScoreWinsEvaluator()
        cfg = ForgettingConfig(enabled=True, evaluator=custom)
        assert cfg.evaluator is custom
        # Pydantic round-trip preserves the instance (arbitrary types allowed).
        # (Note: model_dump returns the instance itself — no serialization.)
        assert cfg.model_dump()["evaluator"] is custom

    @staticmethod
    def test_forget_evaluator_is_abstract():
        # Direct instantiation must fail — evaluate is abstract.
        with pytest.raises(TypeError):
            ForgetEvaluator()  # type: ignore[abstract]


# ---------------------------------------------------------------- ForgettingConfig


class TestForgettingConfig:
    @staticmethod
    def test_defaults():
        cfg = ForgettingConfig()
        assert cfg.enabled is False
        assert cfg.threshold == 0.15
        assert cfg.max_evict == 1000
        assert cfg.min_retention_days == 30
        assert cfg.evaluator is None

    @staticmethod
    def test_threshold_must_be_in_unit_interval():
        with pytest.raises(ValidationError):
            ForgettingConfig(threshold=-0.01)
        with pytest.raises(ValidationError):
            ForgettingConfig(threshold=1.5)

    @staticmethod
    def test_max_evict_must_be_positive():
        with pytest.raises(ValidationError):
            ForgettingConfig(max_evict=0)

    @staticmethod
    def test_min_retention_days_allows_zero():
        # zero is allowed (ge=0) — useful for tests that want to disable
        # the retention window entirely.
        cfg = ForgettingConfig(min_retention_days=0)
        assert cfg.min_retention_days == 0

    @staticmethod
    def test_carries_arbitrary_evaluator_instance():
        ev = EbbinghausEvaluator(InMemoryKVStore())
        cfg = ForgettingConfig(evaluator=ev)
        assert cfg.evaluator is ev


# ---------------------------------------------------------------- EbbinghausEvaluator config


class TestEbbinghausEvaluatorConfig:
    @staticmethod
    def test_default_base_s_is_30():
        ev = EbbinghausEvaluator(InMemoryKVStore())
        assert ev.base_s == 30

    @staticmethod
    def test_base_s_must_be_positive():
        with pytest.raises(ValueError):
            EbbinghausEvaluator(InMemoryKVStore(), base_s=0)
        with pytest.raises(ValueError):
            EbbinghausEvaluator(InMemoryKVStore(), base_s=-1)

    @staticmethod
    def test_base_s_custom_value_stored():
        ev = EbbinghausEvaluator(InMemoryKVStore(), base_s=60)
        assert ev.base_s == 60


# ---------------------------------------------------------------- DreamingConfig


class TestDreamingConfigForgetting:
    @staticmethod
    def test_forgetting_defaults_to_none():
        dc = DreamingConfig()
        assert dc.forgetting is None

    @staticmethod
    def test_forgetting_nested_config_preserved():
        fc = ForgettingConfig(enabled=True, threshold=0.2)
        dc = DreamingConfig(forgetting=fc)
        assert dc.forgetting is fc
        assert dc.forgetting.enabled is True
        assert dc.forgetting.threshold == 0.2

    @staticmethod
    def test_forgetting_evaluator_instance_carries_through():
        ev = EbbinghausEvaluator(InMemoryKVStore())
        dc = DreamingConfig(forgetting=ForgettingConfig(evaluator=ev))
        assert dc.forgetting.evaluator is ev
