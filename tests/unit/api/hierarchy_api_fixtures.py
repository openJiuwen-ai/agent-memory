# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式建树公开 API 测试数据；使用真实装配与存储。"""

from datetime import datetime, timedelta, timezone

from jiuwen_memory.api import EvolveMode, EvolveTaskOptions, HierarchyComposeOptions
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, Scope

HOME = Scope(org="hierarchy-org", user="alice", agent="assistant")
SECURITY = legacy_request_context(HOME)
ROOT_SECURITY = legacy_request_context(Scope())
START = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)


def runtime_config(engine_kind: str) -> dict:
    """复用同名 IndexBuilder，避免候选读源和 Composer 写源脱节。"""
    engine_components = (
        "ingestor", "index_builder", "retriever", "scheduler", "evolver", "lifecycle",
        "job_factory",
    )
    evolution_components = (
        "extractor", "abstractor", "associator", "index_builder", "message_store", "dedup", "llm",
    )
    evolution_params = {component: "default" for component in evolution_components}
    evolution_params["hierarchy_composer"] = "tree"
    return {
        "engine": {"default": {
            "target": engine_kind,
            "params": {component: "default" for component in engine_components},
        }},
        "security": {"default": {"target": "local", "params": {"key_hex": "0" * 64}}},
        "evolver": {"default": {"target": "orchestrating", "params": evolution_params}},
        "hierarchy_composer": {"tree": {"target": "default", "params": {
            "index_builder": "default",
            "hierarchy_profiles": {"time": {
                "leaf_role": "snapshot", "parent_roles": ["time_span"],
            }},
        }}},
    }


def task_options(**overrides) -> EvolveTaskOptions:
    """构造有界 TIME 两层树请求，允许测试逐项覆盖坏输入。"""
    values = {
        "kind": HierarchyKind.TIME,
        "leaf_role": HierarchyRole.SNAPSHOT,
        "parent_roles": [HierarchyRole.TIME_SPAN],
        "tree_home_scope": HOME,
        "span_start": START,
        "span_end": START + timedelta(minutes=1),
    }
    values.update(overrides)
    return EvolveTaskOptions(
        mode=EvolveMode.HIERARCHY, hierarchy_options=HierarchyComposeOptions(**values),
    )


def write_snapshot(api, minute: int, session: str = "conversation"):
    """在指定 session 写入具备提示的权威叶，不启动 infer。"""
    moment = START + timedelta(minutes=minute)
    leaf_scope = Scope(org=HOME.org, user=HOME.user, agent=HOME.agent, session=session)
    return api.add(
        f"snapshot evidence {minute}", leaf_scope, security=SECURITY,
        system_metadata={
            "hierarchy_kind": "time", "hierarchy_role": "snapshot",
            "hierarchy_span_start": moment.isoformat(),
            "hierarchy_span_end": moment.isoformat(), "infer": False,
        },
        user_metadata={"source": f"session-{minute}"},
    )[0]
