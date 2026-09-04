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
from pathlib import Path

from jiuwen_memory.api import assemble
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config

logger = get_logger(__name__)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


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
        security=legacy_request_context(scope),
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
