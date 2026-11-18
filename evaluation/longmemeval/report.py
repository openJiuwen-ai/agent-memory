"""报告渲染：RunResult → JSON（机读/留档）/ Markdown（人读/PR 粘贴）。"""

from __future__ import annotations

from typing import Any

from .types import RunResult


def to_json(run: RunResult) -> dict[str, Any]:
    """机读结构：指标 + 装配摘要 + 逐 case 命中概览（不含正文，控制体积）。"""
    incomplete = run.n_expected > run.n_queries
    return {
        "dataset": run.dataset,
        "n_queries": run.n_queries,
        # 显式区分「应跑」与「实跑」：只看 n_queries 会把跳过的题当成成功。
        "n_expected": run.n_expected or run.n_queries,
        "complete": not incomplete,
        "skipped_queries": [
            qid
            for scope in run.failed_scopes
            for qid in scope.get("query_ids", [])
        ],
        "failed_scopes": run.failed_scopes,
        "config": run.config_summary,
        "metrics": {m.name: {"value": m.value, "detail": m.detail} for m in run.metrics},
        "cases": [
            {
                "query_id": c.query_id,
                "ranked": c.ranked_unit_ids,
                "relevant": sorted(c.relevant_unit_ids),
                "relevant_key_units": c.relevant_key_unit_ids,
                "n_returned": len(c.ranked_unit_ids),
                "memory_retrieval_e2e_wall_ms": c.memory_retrieval_e2e_wall_ms,
                "storage_recall_wall_ms": c.storage_recall_wall_ms,
            }
            for c in run.per_case
        ],
    }


def to_markdown(run: RunResult) -> str:
    """人读报告：装配摘要 + 指标表。"""
    expected = run.n_expected or run.n_queries
    skipped = [
        qid for scope in run.failed_scopes for qid in scope.get("query_ids", [])
    ]
    lines = [
        f"# Eval Report — {run.dataset}",
        "",
        f"- queries: **{run.n_queries}**" + (
            f" / {expected}（**{expected - run.n_queries} 题被跳过，指标不完整**）"
            if expected > run.n_queries
            else ""
        ),
        "- config: " + ", ".join(f"`{k}={v}`" for k, v in run.config_summary.items()),
    ]
    if skipped:
        lines += [
            "",
            f"> ⚠️ 以下 {len(skipped)} 题执行失败被跳过，未计入指标："
            f"`{', '.join(skipped)}`",
        ]
        for scope in run.failed_scopes:
            lines.append(
                f"> - {', '.join(scope.get('query_ids', []))}: "
                f"{scope.get('error_type')} — {str(scope.get('error'))[:120]}"
            )
    lines += [
        "",
        "| metric | value | detail |",
        "| --- | --- | --- |",
    ]
    for m in run.metrics:
        detail = ", ".join(f"{k}={v:g}" for k, v in m.detail.items()) or "-"
        lines.append(f"| {m.name} | {m.value:.4f} | {detail} |")
    return "\n".join(lines)
