# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""construction 模块内的公共实现。

放抽取/分类等算子共享的小工具。此前 ``llm_extractor._parse_tags`` 与
``llm_classifier._parse_tags`` 两处独立维护，语义漂移出三处不一致（纯数字过滤、
去重 key、seen 语义）。集中到本模块，保证两条 LLM 路径对同一 ``tags`` 输入产出
同一结果。

``merge_unit_tags`` 负责把调用方 write tags 与 LLM/系统标记合并进派生 unit，
不对最终列表套 :data:`MAX_TAGS`（``MAX_TAGS`` 只约束 LLM 产出段）。

重要度与来源标识（F10）：LLM 只判重要度类别，数值由 ``importance_category_map``
（globals）查表；原文路径（classifier）只补缺不覆盖调用方显式值，派生路径（抽取/抽象）
总写查表值与 ``origin=inferred``；去重 UPDATE/SUPERSEDE 取 max。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    IMPORTANCE_KEY,
    ORIGIN_INFER,
    ORIGIN_KEY,
    ORIGIN_ORIGINAL,
    ImportanceCategory,
    MemoryUnit,
)

__all__ = [
    "DEFAULT_IMPORTANCE",
    "DEFAULT_IMPORTANCE_CATEGORY_MAP",
    "IMPORTANCE_CATEGORY_MAP_KEY",
    "IMPORTANCE_KEY",
    "MAX_TAGS",
    "ORIGIN_INFER",
    "ORIGIN_KEY",
    "ORIGIN_ORIGINAL",
    "ImportanceCategory",
    "default_importance_for_category",
    "fill_missing_importance",
    "importance_category_map_from_config",
    "importance_category_prompt",
    "importance_category_prompt_enabled_from_config",
    "inferred_attribution",
    "merge_importance_max",
    "merge_unit_tags",
    "parse_importance",
    "parse_importance_category",
    "parse_tags",
    "reject_deprecated_llm_attempt_keys",
    "validate_llm_attempt_policy",
]

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


# ---------------------------------------------------------------------------
# 重要度与来源标识（F10）
# ---------------------------------------------------------------------------

DEFAULT_IMPORTANCE = "0.50"

# 配置键：放 globals（顶层段只收已注册 Producer 名），classifier/extractor/abstractor
# 经 ComponentConfig.get 回退 globals 读到同一份映射。
IMPORTANCE_CATEGORY_MAP_KEY = "importance_category_map"
IMPORTANCE_CATEGORY_PROMPT_ENABLED_KEY = "importance_category_prompt_enabled"

DEFAULT_IMPORTANCE_CATEGORY_MAP: dict[str, str] = {
    ImportanceCategory.IDENTITY.value: "0.95",
    ImportanceCategory.RULE.value: "0.90",
    ImportanceCategory.PREFERENCE.value: "0.85",
    ImportanceCategory.KNOWLEDGE.value: "0.80",
    ImportanceCategory.RELATION.value: "0.75",
    ImportanceCategory.DEFAULT.value: "0.50",
    ImportanceCategory.TRANSIENT.value: "0.30",
}

# 写进 LLM prompt 的类别口径；业务自定义类别只列名字。
_CATEGORY_GUIDE_EN: dict[str, str] = {
    "identity": 'explicit instructions ("remember", "I am", "my name is"), identity, '
    "location, occupation, configuration",
    "rule": 'standing rules or policies ("never", "must", "always")',
    "preference": "preferences or opinions, with their reasons",
    "knowledge": "technical context, decisions or lessons, with their reasoning",
    "relation": "relationships, people, project milestones (with date or status)",
    "transient": 'temporary tasks or momentary state ("run this now", "stop the script")',
    "default": "none of the above clearly applies",
}
_CATEGORY_GUIDE_CN: dict[str, str] = {
    "identity": "明确指令（「记住」「我是」「我叫」）、身份、位置、职业、配置",
    "rule": "站立规则或策略（「永远不要」「必须」「总是」）",
    "preference": "偏好或意见（带理由）",
    "knowledge": "技术上下文、决策或经验（带推理）",
    "relation": "关系、人、项目里程碑（带日期或状态）",
    "transient": "临时任务或瞬时状态（「现在跑这个」「停止脚本」）",
    "default": "以上都不明确适用",
}


def parse_importance(raw: object) -> str:
    """重要度归一：数值裁剪到 [0, 1] 并保留两位；缺失/非数值/非有限值兜底 DEFAULT_IMPORTANCE。"""
    if raw is None or isinstance(raw, bool):
        return DEFAULT_IMPORTANCE
    try:
        value = float(str(raw).strip())
    except ValueError:
        return DEFAULT_IMPORTANCE
    if not math.isfinite(value):
        return DEFAULT_IMPORTANCE
    return f"{min(1.0, max(0.0, value)):.2f}"


def importance_category_map_from_config(config: Any) -> dict[str, str]:
    """装配期读 ``importance_category_map``（本实例 params 优先，回退 globals）。

    缺失取 :data:`DEFAULT_IMPORTANCE_CATEGORY_MAP`；类别名统一小写去空白；值经
    :func:`parse_importance` 归一，防配置笔误引入越界/非数值。非映射直接报错。
    """
    raw = config.get(IMPORTANCE_CATEGORY_MAP_KEY, None) if config is not None else None
    if raw is None:
        return dict(DEFAULT_IMPORTANCE_CATEGORY_MAP)
    if not isinstance(raw, Mapping):
        raise ValidationError(
            f"{IMPORTANCE_CATEGORY_MAP_KEY} must be a mapping of category -> importance; "
            f"got {type(raw).__name__}"
        )
    return {
        str(name).strip().lower(): parse_importance(value)
        for name, value in raw.items()
        if str(name).strip()
    }


def importance_category_prompt_enabled_from_config(config: Any) -> bool:
    """装配期读类别段开关（本实例 params 优先，回退 globals）；缺失为关。

    关时 LLM 系统提示词不追加类别段，LLM 判类被忽略，重要度一律取 ``default`` 类别值。
    """
    raw = config.get(IMPORTANCE_CATEGORY_PROMPT_ENABLED_KEY, None) if config is not None else None
    return as_bool(raw, default=False)


def parse_importance_category(raw: object) -> str:
    """LLM 输出的类别字符串归一（小写去空白）；非字符串视为缺失。"""
    return raw.strip().lower() if isinstance(raw, str) else ""


def default_importance_for_category(
    category: str | None, category_map: Mapping[str, str] | None
) -> str:
    """按类别查表：命中取其值，否则取 ``category_map["default"]``，再兜底 DEFAULT_IMPORTANCE。

    ``category_map`` 为 None（算子直接构造未给配置）时用 :data:`DEFAULT_IMPORTANCE_CATEGORY_MAP`；
    空映射表示配置未提供任何类别，一律 DEFAULT_IMPORTANCE。
    """
    table = DEFAULT_IMPORTANCE_CATEGORY_MAP if category_map is None else category_map
    key = parse_importance_category(category)
    if key and key in table:
        return parse_importance(table[key])
    if ImportanceCategory.DEFAULT.value in table:
        return parse_importance(table[ImportanceCategory.DEFAULT.value])
    return DEFAULT_IMPORTANCE


def inferred_attribution(
    category: str | None, category_map: Mapping[str, str] | None
) -> dict[str, str]:
    """派生记忆的 F10 元数据：总写查表重要度 + ``origin=inferred``。

    放在 ``inherited_system_metadata(...) | {...}`` 的右侧，覆盖从源继承来的值——派生
    记忆的重要度由它自己的类别决定，不沿用源原文的。
    """
    return {
        IMPORTANCE_KEY: default_importance_for_category(category, category_map),
        ORIGIN_KEY: ORIGIN_INFER,
    }


def fill_missing_importance(
    unit: MemoryUnit, category: str | None, category_map: Mapping[str, str] | None
) -> None:
    """原文路径：``importance`` 缺失时按类别查表写入；调用方显式值不覆盖。"""
    if unit.system_metadata.get(IMPORTANCE_KEY) in (None, ""):
        unit.system_metadata[IMPORTANCE_KEY] = default_importance_for_category(
            category, category_map
        )


def merge_importance_max(a: object, b: object) -> str | None:
    """去重合并取 max：一侧缺失/非法取另一侧；两侧都缺返回 None（不写）。"""
    values = [float(parse_importance(v)) for v in (a, b) if _is_importance_value(v)]
    if not values:
        return None
    return f"{max(values):.2f}"


def _is_importance_value(raw: object) -> bool:
    if raw is None or isinstance(raw, bool):
        return False
    try:
        return math.isfinite(float(str(raw).strip()))
    except ValueError:
        return False


def importance_category_prompt(
    category_map: Mapping[str, str] | None, *, language: str = "en"
) -> str:
    """生成追加到 LLM 系统提示词的 ``importance_category`` 字段说明。

    类别集合取自配置映射（缺失时用默认类别），业务追加的类别同步出现在提示词中。
    """
    table = DEFAULT_IMPORTANCE_CATEGORY_MAP if category_map is None else category_map
    names = list(table.keys())
    if ImportanceCategory.DEFAULT.value not in names:
        names.append(ImportanceCategory.DEFAULT.value)
    if language == "cn":
        guide = _CATEGORY_GUIDE_CN
        lines = [
            '另外，每个输出对象都要带 "importance_category" 字段：该条记忆有多重要，'
            "取下列之一（与 tier 无关，独立判断）："
        ]
        lines += [f'  - "{name}"：{guide.get(name, "业务自定义类别")}' for name in names]
        lines.append('拿不准时用 "default"。')
    else:
        guide = _CATEGORY_GUIDE_EN
        lines = [
            'Every output object also carries "importance_category": how important the '
            "memory is, independent of tier. One of:"
        ]
        lines += [f'  - "{name}": {guide.get(name, "business-defined category")}' for name in names]
        lines.append('When unsure, use "default".')
    return "\n".join(lines) + "\n"


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
