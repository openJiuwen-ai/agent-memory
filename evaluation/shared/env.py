"""evaluation 专用环境文件加载。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

EVALUATION_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = EVALUATION_DIR / "environment" / ".env"


def load_evaluation_env(*, override: bool = False) -> Path:
    """加载 ``evaluation/environment/.env``，显式进程变量优先。"""
    load_dotenv(dotenv_path=ENV_FILE, override=override)
    return ENV_FILE


def require_env(*names: str) -> dict[str, str]:
    """读取必需变量；缺失时一次性报告变量名，不输出任何密钥值。"""
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("缺少环境变量: " + ", ".join(missing))
    return values
