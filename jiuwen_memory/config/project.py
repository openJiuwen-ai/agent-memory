# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""把 AssemblyContext 投影为 ConfigSource 可读的扁平 key→str 表。"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.config.context import AssemblyContext, RawSpec
from jiuwen_memory.config.keys import (
    GLOBALS_PREFIX,
    NS_LLM,
    PROMPTS_PREFIX,
    namespaced_key,
    resolve_instance_namespace,
)

_INSTANCE_SCOPED_NAMESPACES = frozenset({NS_LLM})


def _stringify(value: Any) -> str:
    """把装配期标量压成 ConfigSource 可存的字符串（bool → true/false）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def project_assembly_values(ctx: AssemblyContext) -> dict[str, str]:
    """从合并后的装配上下文投影晚绑定配置快照。

    覆盖：
    - ``globals.*``（跳过内部/复杂结构；``prompts`` 单独展开）
    - ``prompts.<phase>.<name>``
    - 已迁移的 ``llm`` 命名空间：default 实例写 ``llm.<field>``，其他实例写
      ``llm.<instance>.<field>``
    - 其他命名空间暂保留旧投影：``<ns>.<instance>.<param>``；default 实例另写
      ``<ns>.<short-field>``。这些命名空间的消费者尚未接入实例级命名空间推导
    - 若 globals 或 params 含 ``<ns>.active`` / ``active`` 约定，照常投影
    """
    out: dict[str, str] = {}

    for key, value in ctx.globals.items():
        if key == "prompts":
            continue
        if key.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            # policies 等复杂结构不进 ConfigSource 主路径（仍归 PolicyManager）
            continue
        out[f"{GLOBALS_PREFIX}{key}"] = _stringify(value)

    prompts = ctx.globals.get("prompts")
    if isinstance(prompts, dict):
        for phase, items in prompts.items():
            if not isinstance(items, dict):
                continue
            for name, text in items.items():
                out[f"{PROMPTS_PREFIX}{phase}.{name}"] = _stringify(text)

    for top_name, instances in ctx.namespaces.items():
        if top_name == "config_source":
            continue
        for inst_name, spec in instances.items():
            if not isinstance(spec, RawSpec):
                continue
            effective_namespace = (
                resolve_instance_namespace(top_name, inst_name, spec.params)
                if top_name in _INSTANCE_SCOPED_NAMESPACES
                else ""
            )
            for param_key, param_val in spec.params.items():
                if isinstance(param_val, (dict, list)):
                    continue
                if effective_namespace:
                    if param_key == "config_namespace":
                        continue
                    field = _short_field(top_name, param_key)
                    out[namespaced_key(effective_namespace, field)] = _stringify(param_val)
                    continue

                out[f"{top_name}.{inst_name}.{param_key}"] = _stringify(param_val)
                if inst_name == "default":
                    short = _short_field(top_name, param_key)
                    out[namespaced_key(top_name, short)] = _stringify(param_val)

    return out


def _short_field(namespace: str, param_key: str) -> str:
    """把 ``embedder_api_key`` 收成约定 ``api_key``；无法收则原样返回。"""
    prefix = f"{namespace}_"
    if param_key.startswith(prefix):
        return param_key[len(prefix):]
    # llm_model → model when namespace llm
    if param_key.startswith("llm_") and namespace == "llm":
        return param_key[len("llm_"):]
    if param_key.startswith("reranker_") and namespace == "reranker":
        return param_key[len("reranker_"):]
    return param_key
