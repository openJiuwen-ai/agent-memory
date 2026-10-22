# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实装配、委托认证与离线显式建树配置。"""

from datetime import timedelta

from jiuwen_memory.api import EvolveMode, EvolveTaskOptions, HierarchyComposeOptions
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, Scope
from tests.unit.api.fixtures import HOME, START, security_for
from tests.unit.api.fixtures import runtime_config as base_config


def runtime_config(engine_kind: str, depth: int = 1) -> dict:
    config = base_config(engine_kind)
    config["engine"]["default"]["params"]["job_factory"] = "default"
    config["globals"] = {"policies": {"hierarchy.enabled": "true"}}
    config["evolver"]["default"]["params"]["hierarchy_composer"] = "tree"
    roles = ["time_span", "scene", "event"][:depth]
    profile = {"leaf_role": "snapshot", "parent_roles": roles}
    config["hierarchy_composer"] = {"tree": {"target": "default", "params": {
        "index_builder": "default", "embedder": "default",
        "hierarchy_profiles": {"time": profile},
    }}}
    return config


def task_options(depth: int = 1, **overrides) -> EvolveTaskOptions:
    values = {
        "kind": HierarchyKind.TIME, "leaf_role": HierarchyRole.SNAPSHOT,
        "parent_roles": [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT][:depth],
        "tree_home_scope": HOME, "span_start": START, "span_end": START + timedelta(minutes=1),
    }
    values.update(overrides)
    return EvolveTaskOptions(mode=EvolveMode.HIERARCHY,
                             hierarchy_options=HierarchyComposeOptions(**values))


def write_snapshot(api, minute: int, session: str = "conversation"):
    moment = START + timedelta(minutes=minute)
    scope = Scope(org=HOME.org, user=HOME.user, agent=HOME.agent, session=session)
    return api.add(f"snapshot evidence {minute}", scope, security=security_for(api, scope),
                   system_metadata={"infer": False}, occurred_at=moment,
                   user_metadata={"source": f"session-{minute}"})[0]
