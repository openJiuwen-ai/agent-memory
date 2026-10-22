# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""周期注册配置及有界等待；使用真实 Scheduler 自有循环。"""

import asyncio

from tests.unit.api.fixtures import HOME
from tests.unit.api.hierarchy_api_fixtures import runtime_config


def background_config(engine_kind: str = "in_memory") -> dict:
    config = runtime_config(engine_kind)
    config["globals"]["policies"]["hierarchy.auto_derive"] = "true"
    config["scheduler"] = {"default": {"target": "async_timer", "params": {"tick_interval": 1}}}
    config["job_factory"] = {"default": {
        "target": "default", "params": {"hierarchy_derive_interval": 1},
    }}
    profile = config["hierarchy_composer"]["tree"]["params"]["hierarchy_profiles"]["time"]
    profile["stage_options"] = {"TimeSpanMerger": {"gap_seconds": "1", "settle_seconds": "1"}}
    return config


async def wait_for_round(api, job_id, security, previous: str = ""):
    async with asyncio.timeout(10):
        while True:
            status = api.job_status(job_id, scope=HOME, security=security)
            if status.detail.get("last_run_id", "") not in ("", previous):
                return status
            await asyncio.sleep(0.02)
