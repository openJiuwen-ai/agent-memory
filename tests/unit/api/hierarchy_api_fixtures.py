# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式建树公开 API 测试数据；使用真实装配与存储。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from jiuwen_memory.api import EvolveMode, EvolveTaskOptions, HierarchyComposeOptions
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, Scope
from jiuwen_memory.config import Config

HOME = Scope(org="hierarchy-org", user="alice", agent="assistant")
SECURITY = legacy_request_context(HOME)
ROOT_SECURITY = legacy_request_context(Scope())
START = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)
TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "examples" / "config_template.yml"
TREE_MARKER = "# ---- TIME 树：显式建树配置 ----"
PERIODIC_MARKER = "# ---- TIME 树周期：追加调度配置 ----"


def hierarchy_template_config(periodic: bool = False) -> Config:
    """按模板注释启用 TIME 配置，仅为测试注入离线密钥与开关。"""
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    tree_section = content.split(TREE_MARKER, 1)[1]
    data.update(parse_template_block(tree_section, "# evolver:", "# gap 是"))
    if periodic:
        periodic_section = content.split(PERIODIC_MARKER, 1)[1]
        data.update(parse_template_block(periodic_section, "# scheduler:", "# 不在 JobFactory"))
    data["globals"]["policies"]["hierarchy.enabled"] = "true"
    data["globals"]["policies"]["hierarchy.auto_derive"] = "true" if periodic else "false"
    data["security"] = {"default": {"target": "local", "params": {"key_hex": "0" * 64}}}
    return Config.from_dict(data)


def parse_template_block(content: str, start: str, end: str) -> dict:
    """只去除配置块的外层注释，保留内部可选模型注释。"""
    block = start + content.split(start, 1)[1].split(end, 1)[0]
    decoded = "\n".join(line[2:] for line in block.splitlines() if line.startswith("# "))
    return yaml.safe_load(decoded)


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
