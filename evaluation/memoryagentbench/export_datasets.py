"""把 MemoryAgentBench 的 `Conflict_Resolution` 原始 parquet 拆成单子集 JSON。

官方数据集：HuggingFace `ai-hyz/MemoryAgentBench` 的
`Conflict_Resolution-00000-of-00001.parquet`（8 行 = 8 个子集：
`factconsolidation_{mh,sh}_{6k,32k,64k,262k}`，四列 context/questions/answers/metadata）。

用法：
  1. 把 parquet 放到本目录（`evaluation/datasets/memoryagentbench/`）；
  2. .venv/bin/python -m evaluation.memoryagentbench.export_datasets

导出结果写到同目录，每个子集一个 `<metadata.source>.json`，格式为
`[ {context, questions, answers, metadata} ]`（`run.py` 取第 0 个元素）。
本仓默认只提交 `factconsolidation_mh_6k.json`，其余子集由 `.gitignore` 排除。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO / "evaluation" / "datasets" / "memoryagentbench"
PARQUET = OUT_DIR / "Conflict_Resolution-00000-of-00001.parquet"


def _jsonable(value: Any) -> Any:
    """ndarray / numpy 标量 → list / 原生标量（递归）。"""
    if hasattr(value, "tolist"):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def main() -> int:
    if not PARQUET.exists():
        raise SystemExit(
            f"缺少原始数据集：{PARQUET}\n"
            "请从 HuggingFace `ai-hyz/MemoryAgentBench` 下载 "
            "`Conflict_Resolution-00000-of-00001.parquet` 放到该路径后重试。"
        )
    rows = pq.read_table(str(PARQUET)).to_pylist()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for row in rows:
        metadata = _jsonable(row.get("metadata") or {})
        source = str((metadata or {}).get("source") or "").strip()
        if not source:
            logger.warning("skip row: metadata.source 缺失")
            continue
        context = row["context"]
        payload = [
            {
                "context": context,
                "questions": _jsonable(row["questions"]),
                "answers": _jsonable(row["answers"]),
                "metadata": metadata,
            }
        ]
        out = OUT_DIR / f"{source}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        written.append(
            (source, len(context), context.count("\n") + 1, len(row["questions"]))
        )
        logger.info("wrote %s", out.name)

    logger.info("子集              字符数     事实数    QA")
    for source, chars, facts, qa in written:
        logger.info("%-28s%9d%8d%6d", source, chars, facts, qa)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
