"""报告渲染：RunResult → JSON（机读/留档）/ Markdown（人读/PR 粘贴）。"""

from __future__ import annotations

from typing import Any, Dict

from .types import RunResult


def to_json(run: RunResult) -> Dict[str, Any]:
    """机读结构：指标 + 装配摘要 + 逐 case 命中概览（不含正文，控制体积）。"""
    return {
        "dataset": run.dataset,
        "n_queries": run.n_queries,
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
    lines = [
        f"# Eval Report — {run.dataset}",
        "",
        f"- queries: **{run.n_queries}**",
        "- config: " + ", ".join(f"`{k}={v}`" for k, v in run.config_summary.items()),
        "",
        "| metric | value | detail |",
        "| --- | --- | --- |",
    ]
    for m in run.metrics:
        detail = ", ".join(f"{k}={v:g}" for k, v in m.detail.items()) or "-"
        lines.append(f"| {m.name} | {m.value:.4f} | {detail} |")
    return "\n".join(lines)
