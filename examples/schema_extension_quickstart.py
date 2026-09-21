"""Run one real Entity Schema extraction through the isolated mem2.0 extension.

PowerShell::

    $env:LLM_API_KEY="xxx"
    $env:LLM_BASE_URL="http://your-openai-compatible-service/v1"
    $env:LLM_MODEL="your-model"
    $env:PYTHONPATH="$PWD"
    python examples/schema_extension_quickstart.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from jiuwen_memory.api import Surface, assemble
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security import new_request_context
from jiuwen_memory.common.security.types import AuthContext, Role
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config

logger = get_logger(__name__)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _dev_security(actor: Scope):
    """演示用可信上下文（**开发模式**，仅限无网络对端的进程内示例）。

    示例进程即自己的 composition root，身份由本进程显式声明；生产部署的身份必须
    由认证边界（API Key / Trusted Gateway）产出，不得自述。
    """
    return new_request_context(
        AuthContext(
            actor=actor,
            role=Role.USER,
            credential_type="internal",
            auth_method="internal",
            authenticated_at=datetime.now(timezone.utc),
        ),
        surface=Surface.INTERNAL,
    )


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    schema_path = repo_root / "examples" / "persona.json"
    config = Config.from_dict(
        {
            "globals": {
                "schema_enabled": True,
                "vector_enabled": True,
                "graph_enabled": False,
                "rerank_enabled": False,
                "llm_api_key": _required_env("LLM_API_KEY"),
                "llm_base_url": _required_env("LLM_BASE_URL"),
                "llm_model": _required_env("LLM_MODEL"),
            },
            "llm": {"default": {"target": "openai"}},
            "extractor": {
                "default": {
                    "target": "entity_schema",
                    "params": {
                        "schema_path": str(schema_path),
                        "llm": "default",
                        "enable_schema_selection": True,
                        "schema_validation_attempts": 3,
                    },
                }
            },
            "evolver": {
                "default": {
                    "target": "schema_orchestrating",
                    "params": {
                        "extractor": "default",
                        "llm": "default",
                    },
                }
            },
        }
    )
    api = assemble(config=config)
    scope = Scope(org="schema-demo", user="alice")
    units = api.add(
        "speaker=Alice: On 2023-08-03, I started working as a software engineer at Acme.",
        scope,
        security=_dev_security(scope),
        system_metadata={"infer": True},
        user_metadata={"example": "schema_extension_quickstart"},
    )
    logger.info(
        "%s",
        json.dumps(
            [
                {
                    "id": unit.id,
                    "content": unit.content,
                    "entities": unit.entities,
                    "system_metadata": unit.system_metadata,
                    "user_metadata": unit.user_metadata,
                }
                for unit in units
            ],
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
    )


if __name__ == "__main__":
    main()
