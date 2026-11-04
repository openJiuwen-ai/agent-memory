# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""construction 模块内的公共实现。

放抽取/分类等算子共享的小工具。此前 ``llm_extractor._parse_tags`` 与
``llm_classifier._parse_tags`` 两处独立维护，语义漂移出三处不一致（纯数字过滤、
去重 key、seen 语义）。集中到本模块，保证两条 LLM 路径对同一 ``tags`` 输入产出
同一结果。

``merge_unit_tags`` 负责把调用方 write tags 与 LLM/系统标记合并进派生 unit，
不对最终列表套 :data:`MAX_TAGS`（``MAX_TAGS`` 只约束 LLM 产出段）。

``resolve_message_store`` 是原文 KV 的装配兜底，evolver 与经验记忆的评价/抽取
算子装配共用（mem2.0 §2.2）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.errors import ValidationError

if TYPE_CHECKING:
    from jiuwen_memory.storage.kv import KVStore

# LLM 抽的 tags 上限（prompt 要求 1-3 个，解析端兜底截断）。
MAX_TAGS = 3

_DEPRECATED_LLM_ATTEMPT_KEYS = {
    "extractor": {"extractor_retry_max": "extractor_max_attempts"},
    "abstractor": {"abstractor_retry_max": "abstractor_max_attempts"},
    "associator": {"associator_retry_max": "associator_max_attempts"},
    "classifier": {"classifier_retry_max": "classifier_max_attempts"},
    "layer_annotator": {
        "layer_annotator_retry_max": "layer_annotator_max_attempts",
    },
    "router": {
        "retry_max_retries": "router_max_attempts",
        "retry_backoff_ms": "router_retry_backoff",
    },
}


def reject_deprecated_llm_attempt_keys(config: Any, component: str) -> None:
    """拒绝 construction LLM 尝试次数旧键，避免升级后被静默忽略。"""
    renames = _DEPRECATED_LLM_ATTEMPT_KEYS[component]
    params = getattr(config, "params", None)
    globals_ = getattr(getattr(config, "ctx", None), "globals", None)
    scopes = [scope for scope in (params, globals_) if isinstance(scope, Mapping)]
    found = [old for old in renames if any(old in scope for scope in scopes)]
    if not found:
        return
    details = "; ".join(f"{old!r} -> {renames[old]!r}" for old in found)
    raise ValidationError(
        f"Deprecated construction LLM attempt config keys are not supported: {details}"
    )


def validate_llm_attempt_policy(
    max_attempts: int | str, retry_backoff_ms: int | str,
) -> tuple[int, int]:
    """总尝试次数 >= 1、退避毫秒数 >= 0；支持配置变量展开后的整数字符串。"""
    values = []
    for name, raw, minimum in (
        ("max_attempts", max_attempts, 1),
        ("retry_backoff_ms", retry_backoff_ms, 0),
    ):
        message = f"{name} must be an integer >= {minimum}; got {raw!r}"
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValidationError(message)
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValidationError(message) from exc
        if value < minimum:
            raise ValidationError(message)
        values.append(value)
    return values[0], values[1]


def parse_tags(raw) -> list[str]:
    """解析 LLM 输出的 tags：清洗（strip/去空/去纯数字/大小写不敏感去重）+ 截断到 ≤3。

    非法输入（非 list、元素非 str）容错：逐项 str() 化后 strip；空串/纯空白/纯数字丢弃；
    保留首次出现的（按 ``s.lower()`` 去重、保序）；最多取前 :data:`MAX_TAGS` 个。
    """
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s or s.isdigit():
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(s)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def merge_unit_tags(*tag_lists: list[str]) -> list[str]:
    """多路 tags 合并：保序、大小写不敏感去重；不对最终结果套 :data:`MAX_TAGS`。

    典型顺序：write tags（调用方）→ LLM/主题 tags → 系统标记（``extracted`` /
    ``procedural``）。空串与纯空白丢弃；同 key 保留首次出现的原样写法。
    """
    seen: set[str] = set()
    out: list[str] = []
    for tags in tag_lists:
        for item in tags:
            s = str(item).strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
    return out


def resolve_message_store(config: Any) -> KVStore:
    """取原文 store：params 声明了就按引用取，没声明则复用 ``kv_store.default`` 具名实例。

    不能简单用 ``KvProducer.dep(config, "message_store", default="memory")``——``dep`` 的
    ``default`` 分支是**匿名新建**，会造出一个与配置后端无关的进程内 KV，原文从此写进谁也
    看不见的 dict（不落盘、重启即失、``storage.scan`` 查不到、无告警）。

    而字段缺失是常态：``AssemblyContext.merged`` 按具名实例整体覆盖，用户一旦声明
    ``evolver.default``（如 examples/config_template.yml 里启用 dynamic 的写法），内置
    params 里的 ``message_store`` 就随之消失。

    与 :meth:`StoreManagerProducer.resolve` 同款兜底——两者都是**有状态**依赖，新建一个等于换后端。

    mem2.0 起经验的评价算子与抽取算子装配也用它取轨迹所在 KV（轨迹记录、判定记录
    与原文同一后端，§2.2）。
    """
    from jiuwen_memory.storage.kv import KvProducer

    if "message_store" in config.params:
        return KvProducer.dep(config, "message_store")
    if "default" in config.ctx.namespaces.get(KvProducer.TOP_NAME, {}):
        return KvProducer.build_named("default", config.ctx)
    return KvProducer.build("memory", {}, config.ctx)
