"""在导入 SSH LoCoMo v5 runner 前注入本地功能测试配置。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from evaluation.shared.env import EVALUATION_DIR, load_evaluation_env


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LoCoMo v5 单会话端到端测评")
    parser.add_argument(
        "--data",
        default=str(EVALUATION_DIR / "datasets" / "locomo" / "locomo10.json"),
    )
    parser.add_argument("--conversation-ids", default="0", help="逗号分隔的会话数组下标")
    parser.add_argument("--max-sessions", type=int, default=0)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--max-qa", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--recall-top-k", type=int, default=200)
    parser.add_argument("--cutoffs", default="10,20,50,200")
    parser.add_argument("--run-tag", default="local-smoke")
    parser.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--scope-tag",
        default="",
        help="仅在复用已写入数据时填写 run_meta.json 中的 SCOPE_TAG",
    )
    parser.add_argument("--skip-write", action="store_true")
    parser.add_argument("--cleanup-after-run", action="store_true")
    parser.add_argument("--output-root", default=str(EVALUATION_DIR / "outputs" / "locomo"))
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yml")))
    return parser


def main(argv: list[str] | None = None) -> int:
    load_evaluation_env()
    args = _build_parser().parse_args(argv)
    data_path = Path(args.data).resolve()
    config_path = Path(args.config).resolve()
    if not data_path.is_file():
        raise RuntimeError(f"数据文件不存在: {data_path}")
    if not config_path.is_file():
        raise RuntimeError(f"配置文件不存在: {config_path}")
    run_id = args.run_id
    if not run_id:
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
        run_id = f"{timestamp}-{uuid4().hex[:8]}"
    scope_tag = args.scope_tag.strip()
    if args.skip_write and not scope_tag:
        raise RuntimeError("--skip-write 必须同时指定 --scope-tag，避免复用错误的 Scope")
    if scope_tag and not args.skip_write:
        raise RuntimeError("--scope-tag 仅能与 --skip-write 一起用于显式复用旧 Scope")
    if args.skip_write and args.cleanup_after_run:
        raise RuntimeError("复用旧 Scope 时不能开启 --cleanup-after-run")

    overrides = {
        "INPUT_DATA_FILE": str(data_path),
        "CONV_IDS": args.conversation_ids,
        "MAX_SESSIONS": str(args.max_sessions),
        "MAX_TURNS": str(args.max_turns),
        "MAX_QA": str(args.max_qa),
        "CONCURRENCY": str(args.concurrency),
        "RECALL_TOP_K": str(args.recall_top_k),
        "CUTOFFS": args.cutoffs,
        "RUN_TAG": args.run_tag,
        "RUN_ID": run_id,
        "SCOPE_TAG": scope_tag,
        "SKIP_WRITE": "1" if args.skip_write else "0",
        "CLEANUP_AFTER_RUN": "1" if args.cleanup_after_run else "0",
        "RUNS_ROOT": str(Path(args.output_root).resolve()),
        "CONFIG_FILE": str(config_path),
        "RESUME": "0",
    }
    os.environ.update(overrides)

    from evaluation.locomo.run import main as run_main

    return run_main()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
