"""断点续跑的行为测试。

设计原则（v2）：
- 断点 key 用 query_id，不用位置下标 —— 位置下标会随 --samples/--max-questions 变化。
- 命中前必须校验：恢复的结果覆盖本次请求的 query_id 集合，否则视为未命中并重跑。
- 断点损坏/残缺/配置不符 —— 一律重跑，绝不产生虚假成功。
- 落盘失败不得丢弃已算结果（与 scope 失败解耦）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.longmemeval.checkpoint import (
    CheckpointError,
    checkpoint_decode,
    checkpoint_encode,
    load_checkpoint,
    save_checkpoint,
)
from evaluation.longmemeval.types import CaseOutcome
from jiuwen_memory.retrieval.types import RecallChannel, TrajectoryStep


def _outcome(query_id: str = "q1") -> CaseOutcome:
    return CaseOutcome(
        query_id=query_id,
        query_text="what?",
        ranked_unit_ids=["u1", "u2"],
        relevant_unit_ids={"u1", "u3"},
        contents=["c1", "c2"],
        context_dates=["2023-01-01T00:00:00"],
        trajectory=[
            TrajectoryStep(
                stage="recall",
                channel=RecallChannel.VECTOR,
                candidate_count=7,
                cost_ms=1.25,
                detail={"k": "80"},
            )
        ],
        context_message_dates=["2023-01-01T00:00:00"],
        context_event_dates=["2023-01-01T00:00:00"],
        memory_retrieval_e2e_wall_ms=11.5,
        storage_recall_wall_ms=4.25,
        expected_answer="X",
        metadata={"bucket": "multi-session"},
        relevant_key_unit_ids={"k1": ["u1"]},
    )


# --------------------------------------------------------------- 编码往返


def test_encode_decode_round_trip_is_lossless() -> None:
    original = _outcome("gpt4_abc")
    restored = checkpoint_decode(checkpoint_encode(original))
    assert restored == original, "往返必须完全相等"
    assert isinstance(restored.relevant_unit_ids, set), "set 类型必须保持"
    assert isinstance(restored.trajectory[0], TrajectoryStep), "嵌套 dataclass 必须恢复"
    assert restored.trajectory[0].channel is RecallChannel.VECTOR, "枚举必须恢复"


def test_encode_handles_empty_and_exotic_values() -> None:
    case = _outcome("q_空 set")
    case.relevant_unit_ids = set()
    case.contents = ["", 'quote " and \\ backslash', "换行\n与 emoji 🎯"]
    case.expected_answer = None  # type: ignore[assignment]
    restored = checkpoint_decode(checkpoint_encode(case))
    assert restored == case


def test_encode_rejects_unknown_type_instead_of_silently_mangling() -> None:
    with pytest.raises(CheckpointError):
        checkpoint_encode({"bad": object()})


# --------------------------------------------------------------- 存储与校验


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    outcome = _outcome("gpt4_x")
    save_checkpoint(tmp_path, "fp1", [outcome])
    loaded = load_checkpoint(tmp_path, "fp1", ["gpt4_x"])
    assert loaded is not None
    assert [o.query_id for o in loaded] == ["gpt4_x"]
    assert loaded[0] == outcome


def test_load_returns_none_when_fingerprint_differs(tmp_path: Path) -> None:
    save_checkpoint(tmp_path, "fp1", [_outcome("q1")])
    assert load_checkpoint(tmp_path, "fp2", ["q1"]) is None, "配置变了必须视为未命中"


def test_load_returns_none_when_requested_ids_not_covered(tmp_path: Path) -> None:
    """关键回归：断点里只有 q1，却请求 q1+q2 时不得只返回 q1 当成功。"""
    save_checkpoint(tmp_path, "fp1", [_outcome("q1")])
    assert load_checkpoint(tmp_path, "fp1", ["q1", "q2"]) is None


def test_load_returns_none_when_empty_results(tmp_path: Path) -> None:
    """关键回归：空断点不得被当成已完成（会产出 n_queries=0 的假成功）。

    通过手写文件构造该状态——save_checkpoint 自身拒绝写空断点。
    """
    path = tmp_path / "ckpt_any.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "fingerprint": "fp1", "query_ids": ["q1"], "results": []}
        ),
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None


def test_load_returns_none_for_corrupt_files(tmp_path: Path) -> None:
    path = tmp_path / "ckpt.json"
    corrupt_samples = (
        '{"truncated": ',
        "[]",
        '"text"',
        "42",
        "null",
        '{"fingerprint":"fp1","results":"x"}',
    )
    for bad in corrupt_samples:
        path.write_text(bad, encoding="utf-8")
        assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None, f"坏文件必须视为未命中: {bad}"


def test_load_returns_none_when_stored_id_mismatches(tmp_path: Path) -> None:
    """断点声称有 q1，内容却是别的题 —— 必须拒绝。"""
    path = tmp_path / "ckpt.json"
    path.write_text(
        json.dumps(
            {
                "fingerprint": "fp1",
                "results": [{"outcome": checkpoint_encode(_outcome("q_other"))}],
            }
        ),
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None


def test_save_is_atomic_and_leaves_no_partial_file(tmp_path: Path) -> None:
    save_checkpoint(tmp_path, "fp1", [_outcome("q1")])
    files = list(tmp_path.iterdir())
    assert len(files) == 1, f"不应残留临时文件: {[f.name for f in files]}"
    assert files[0].suffix == ".json"


def test_save_failure_raises_but_does_not_corrupt_existing(tmp_path: Path) -> None:
    save_checkpoint(tmp_path, "fp1", [_outcome("q1")])
    before = next(tmp_path.iterdir()).read_text(encoding="utf-8")
    with pytest.raises(CheckpointError):
        save_checkpoint(tmp_path, "fp1", [{"not": "a CaseOutcome"}])  # type: ignore[list-item]
    after = next(f for f in tmp_path.iterdir() if f.suffix == ".json").read_text(encoding="utf-8")
    assert after == before, "写失败不得破坏已有断点"


# --------------------------------------------------------------- 并发安全


def test_missing_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper"
    save_checkpoint(target, "fp1", [_outcome("q1")])
    assert load_checkpoint(target, "fp1", ["q1"]) is not None


def test_load_returns_none_for_empty_request(tmp_path: Path) -> None:
    """请求空题目集合时不做任何读取。"""
    assert load_checkpoint(tmp_path, "fp1", []) is None


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_checkpoint(tmp_path, "fp1", ["absent"]) is None


def test_load_returns_none_on_schema_version_mismatch(tmp_path: Path) -> None:
    save_checkpoint(tmp_path, "fp1", [_outcome("q1")])
    assert load_checkpoint(tmp_path, "fp1", ["q1"], schema_version=99) is None


def test_load_returns_none_when_query_ids_field_missing(tmp_path: Path) -> None:
    path = tmp_path / "ckpt_x.json"
    path.write_text(
        json.dumps({"schema_version": 1, "fingerprint": "fp1", "results": []}),
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None


def test_load_returns_none_when_results_not_list(tmp_path: Path) -> None:
    path = tmp_path / "ckpt_y.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fingerprint": "fp1",
                "query_ids": ["q1"],
                "results": "not-a-list",
            }
        ),
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None


def test_save_then_load_survives_mixed_type_set(tmp_path: Path) -> None:
    """混合类型集合不可排序，仍应可往返（按 repr 兜底排序）。"""
    case = _outcome("q_mixed")
    case.relevant_unit_ids = {1, "a"}  # type: ignore[assignment]
    save_checkpoint(tmp_path, "fp1", [case])
    loaded = load_checkpoint(tmp_path, "fp1", ["q_mixed"])
    assert loaded is not None
    assert loaded[0].relevant_unit_ids == {1, "a"}


def test_save_rejects_non_outcome_payload(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError):
        save_checkpoint(tmp_path, "fp1", [{"not": "outcome"}])  # type: ignore[list-item]


def test_fingerprint_is_stable_and_order_independent() -> None:
    from evaluation.longmemeval.checkpoint import fingerprint_of

    first = fingerprint_of(b=2, a=1, c=[1, 2])
    second = fingerprint_of(a=1, c=[1, 2], b=2)
    assert first == second, "键序不应影响指纹"
    assert first != fingerprint_of(a=1, b=3, c=[1, 2]), "值变化必须改变指纹"


def test_tuple_values_round_trip(tmp_path: Path) -> None:
    """tuple 编码为显式标签，解码后仍为 tuple。"""
    from evaluation.longmemeval.checkpoint import checkpoint_decode, checkpoint_encode

    case = _outcome("q_tuple")
    case.metadata = {"pair": (1, 2)}  # type: ignore[dict-item]
    restored = checkpoint_decode(checkpoint_encode(case))
    assert restored.metadata["pair"] == (1, 2)
    assert isinstance(restored.metadata["pair"], tuple)


# ------------------------------------------------- 杀死剩余变异（ub-mode mutator）


def test_load_results_field_of_wrong_type_is_rejected(tmp_path: Path) -> None:
    """results 非 list 或为空都必须重跑（覆盖 isinstance 与 or 的短路）。"""
    base = {"schema_version": 1, "fingerprint": "fp1", "query_ids": ["q1"]}
    for bad_results in ("not-a-list", [], {}):
        path = tmp_path / "ckpt_t.json"
        path.write_text(json.dumps({**base, "results": bad_results}), encoding="utf-8")
        assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None, f"results={bad_results!r}"


def test_load_rejects_query_ids_mismatch_with_extra_entry(tmp_path: Path) -> None:
    """断点声明的题目集合与请求不同 —— 必须拒绝，不能只比对子集。"""
    path = tmp_path / "ckpt_u.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fingerprint": "fp1",
                "query_ids": ["q1", "q2"],
                "results": [checkpoint_encode(_outcome("q1"))],
            }
        ),
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None


def test_save_cleans_up_temp_file_on_success(tmp_path: Path) -> None:
    """成功后不得残留临时文件（覆盖 finally 的 unlink 分支）。"""
    save_checkpoint(tmp_path, "fp1", [_outcome("q1")])
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"残留临时文件: {leftovers}"


def test_save_cleans_up_temp_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """replace 失败时也要清理临时文件，并抛 CheckpointError。"""
    original = Path.replace

    def explode(self: Path, target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", explode)
    try:
        with pytest.raises(CheckpointError):
            save_checkpoint(tmp_path, "fp1", [_outcome("q1")])
    finally:
        monkeypatch.setattr(Path, "replace", original)
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"失败后残留临时文件: {leftovers}"


def test_save_tolerates_missing_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """临时文件已被外部删除时，清理步骤不得抛错（覆盖 missing_ok 分支）。"""
    original_replace = Path.replace

    def replace_then_delete(self: Path, target: Path) -> None:
        original_replace(self, target)
        # 模拟：replace 后临时文件已不存在，finally 仍会尝试 unlink
        assert not self.exists()

    monkeypatch.setattr(Path, "replace", replace_then_delete)
    save_checkpoint(tmp_path, "fp1", [_outcome("q1")])  # 不得抛异常


def test_save_preserves_non_ascii_content(tmp_path: Path) -> None:
    """中文/emoji 必须原样保存（覆盖 ensure_ascii 分支）。"""
    case = _outcome("q_中文")
    case.expected_answer = "答案含中文与 emoji 🎯"
    save_checkpoint(tmp_path, "fp1", [case])
    loaded = load_checkpoint(tmp_path, "fp1", ["q_中文"])
    assert loaded is not None
    assert loaded[0].expected_answer == "答案含中文与 emoji 🎯"
    raw = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "答案含中文" in raw, "文件应保留可读的中文字符而非 \\u 转义"


def test_load_rejects_empty_results_list(tmp_path: Path) -> None:
    """results 是空 list 时必须重跑（覆盖 `not raw_results` 判断）。"""
    path = tmp_path / "ckpt_v.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fingerprint": "fp1",
                "query_ids": ["q1"],
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None


def test_load_rejects_payload_without_query_ids_field(tmp_path: Path) -> None:
    """payload 缺 query_ids 时按空集合比对 → 与请求不符 → 重跑。"""
    path = tmp_path / "ckpt_w.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fingerprint": "fp1",
                "results": [checkpoint_encode(_outcome("q1"))],
            }
        ),
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path, "fp1", ["q1"]) is None





# ------------------------------------------------ 并发场景（用户真实用法）
# 用户场景：45 题跑 8 小时，必须支持「并发 + 断点」。
# 以下测试从 Harness 的角度验证：并发执行时断点是否生效。


def _make_harness(tmp_path: Path, fingerprint: str = "fp"):
    """构造一个不装配真实内核的 harness 骨架，只测 evaluate() 的调度逻辑。"""
    from unittest.mock import MagicMock, patch

    from evaluation.longmemeval.harness import EvalHarness

    harness = object.__new__(EvalHarness)
    harness._checkpoint_dir = tmp_path
    harness._checkpoint_fingerprint = fingerprint
    harness._artifact_dir = None
    harness._enable_timing = True
    harness._failed_scopes = []
    harness._scope_key = lambda scope: tuple(scope)
    return harness


def test_concurrent_evaluate_writes_checkpoint(tmp_path: Path) -> None:
    """并发执行必须落盘断点 —— 否则中途崩溃会丢失所有已完成工作。"""
    from unittest.mock import MagicMock, patch

    from evaluation.longmemeval.harness import EvalHarness

    harness = _make_harness(tmp_path)
    calls: list[str] = []

    def fake_scope(seeds, indexed_cases):
        qid = indexed_cases[0][1].query_id
        calls.append(qid)
        return [(index, _outcome(qid)) for index, _ in indexed_cases]

    dataset = MagicMock()
    dataset.seeds.return_value = []
    dataset.queries.return_value = [
        MagicMock(query_id=f"q{i}", scope=(f"scope{i}",)) for i in range(4)
    ]

    with patch.object(EvalHarness, "_evaluate_scope", side_effect=fake_scope):
        harness.evaluate(dataset, concurrency=2)

    saved = list(tmp_path.glob("ckpt_*.json"))
    assert saved, "并发执行后必须有断点文件，否则崩溃即全丢"


def test_concurrent_evaluate_reuses_checkpoint(tmp_path: Path) -> None:
    """并发续跑必须跳过已完成的题 —— 否则前一次跑的工作全部重做。"""
    from unittest.mock import MagicMock, patch

    from evaluation.longmemeval.harness import EvalHarness

    # 预先写好 q0/q1 的断点
    save_checkpoint(tmp_path, "fp", [_outcome("q0"), _outcome("q1")])

    harness = _make_harness(tmp_path)
    executed: list[str] = []

    def fake_scope(seeds, indexed_cases):
        qid = indexed_cases[0][1].query_id
        executed.append(qid)
        return [(index, _outcome(qid)) for index, _ in indexed_cases]

    dataset = MagicMock()
    dataset.seeds.return_value = []
    dataset.queries.return_value = [
        MagicMock(query_id=f"q{i}", scope=(f"scope{i}",)) for i in range(4)
    ]

    with patch.object(EvalHarness, "_evaluate_scope", side_effect=fake_scope):
        outcomes = harness.evaluate(dataset, concurrency=2)

    assert set(executed) == {"q2", "q3"}, f"应只执行未完成的题，实际执行了 {executed}"
    assert len(outcomes) == 4, "应返回全部 4 题（2 断点 + 2 新跑）"


def test_concurrent_crash_then_resume_does_not_redo_finished(tmp_path: Path) -> None:
    """端到端场景：并发跑到一半崩溃，重启后只跑未完成的题。

    这是线上真实故障的复现（45 题跑 8 小时，崩溃后断点 0 个、全部重做）。
    之前从未构造过这个场景，导致并发分支缺断点被漏掉。
    """
    from unittest.mock import MagicMock, patch

    from evaluation.longmemeval.harness import EvalHarness

    dataset = MagicMock()
    dataset.seeds.return_value = []
    dataset.queries.return_value = [
        MagicMock(query_id=f"q{i}", scope=(f"scope{i}",)) for i in range(6)
    ]

    # ---- 第一次运行：跑到第 3 个 scope 时崩溃 ----
    harness1 = _make_harness(tmp_path)
    executed_1: list[str] = []

    def crash_on_q2(seeds, indexed_cases):
        qid = indexed_cases[0][1].query_id
        executed_1.append(qid)
        if qid == "q2":
            raise RuntimeError("simulated crash")
        return [(index, _outcome(qid)) for index, _ in indexed_cases]

    with patch.object(EvalHarness, "_evaluate_scope", side_effect=crash_on_q2):
        harness1.evaluate(dataset, concurrency=2)

    # 崩溃前完成的题必须已落盘
    saved = {p.name for p in tmp_path.glob("ckpt_*.json")}
    assert len(saved) >= 2, f"崩溃前应已落盘部分题目，实际 {saved}"

    # ---- 第二次运行：续跑，已完成的题不得重做 ----
    harness2 = _make_harness(tmp_path)
    executed_2: list[str] = []

    def normal(seeds, indexed_cases):
        qid = indexed_cases[0][1].query_id
        executed_2.append(qid)
        return [(index, _outcome(qid)) for index, _ in indexed_cases]

    with patch.object(EvalHarness, "_evaluate_scope", side_effect=normal):
        outcomes = harness2.evaluate(dataset, concurrency=2)

    assert len(outcomes) == 6, f"最终应有全部 6 题，实际 {len(outcomes)}"
    overlap = set(executed_1) & set(executed_2) - {"q2"}
    assert not overlap, f"已完成的题不应重做，重复执行了 {overlap}"
