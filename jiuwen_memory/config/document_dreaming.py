# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared document Dreaming settings for long-lived memory hosts."""

import os
from copy import deepcopy
from pathlib import Path

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config.document_flag import should_write_document

_DEFAULT_INTERVAL = 4 * 60 * 60


def document_root(config: dict | None) -> str:
    store = (config or {}).get("markdown_store", {}).get("default")
    root = store.get("params", {}).get("root") if isinstance(store, dict) else None
    return os.path.normcase(str(Path(root).expanduser().resolve())) if root else ""


def consolidation_interval(config: dict) -> int | None:
    globals_config = config.get("globals", {})
    settings = globals_config.get("memory_consolidation", {})
    if not isinstance(settings, dict):
        raise ValidationError("globals.memory_consolidation must be a mapping")
    if settings.get("enabled", True) is not True:
        return None
    if not document_root(config) or not should_write_document(globals_config.get("write_document")):
        return None
    interval = settings.get("interval_seconds", _DEFAULT_INTERVAL)
    if type(interval) is not int or interval <= 0:
        raise ValidationError("Memory consolidation interval_seconds must be a positive integer")
    return interval


def with_consolidation_scheduler(config: dict) -> dict:
    interval = consolidation_interval(config)
    if interval is None:
        return config
    root = document_root(config)
    config = deepcopy(config)
    globals_config = config.setdefault("globals", {})
    if not globals_config.get("dreaming_registry_store"):
        config.setdefault("kv_store", {})["memory_background"] = {
            "target": "sqlite",
            "params": {
                "db_path": str(Path(root) / "memory/.shadow/memory-dreaming.db"),
                "fixed_path": True,
            },
        }
        globals_config["dreaming_registry_store"] = "memory_background"
    schedulers = config.setdefault("scheduler", {})
    scheduler = schedulers.get("default", "in_process")
    target = scheduler.get("target") if isinstance(scheduler, dict) else scheduler
    if target == "in_process":
        schedulers["default"] = {
            "target": "async_timer", "params": {"tick_interval": min(10, interval)},
        }
    return config
