"""出厂默认配置（config/defaults.py）中 retrieval F09 / construction F10 相关项的契约测试。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config.defaults import default_config_dict
from jiuwen_memory.retrieval.bootstrap import register_operators

pytestmark = pytest.mark.unit


def test_default_secondary_scorer_present() -> None:
    spec = default_config_dict()["secondary_scorer"]["default"]
    assert spec["target"] == "default"
    assert spec["params"] == {
        "score_band": 0.05,
        "importance_weight": 0.5,
        "recency_weight": 0.5,
        "recency_tau_days": 30.0,
    }


def test_secondary_rank_enabled_default_false() -> None:
    raw = default_config_dict()
    # 总开关与 rerank_enabled 同放 globals；留在 retriever params 会压住用户写在 globals 的 true。
    assert raw["globals"]["secondary_rank_enabled"] is False
    params = raw["retriever"]["default"]["params"]
    assert "secondary_rank_enabled" not in params
    assert params["secondary_scorer"] == "default"


def test_secondary_scorer_is_known_top_name() -> None:
    register_operators()
    # 顶层段名须先注册，否则用户 YAML 里的 secondary_scorer 段会被解析期当未知段拒掉。
    assert "secondary_scorer" in Factory.known_top_names()


def test_default_importance_category_map_present() -> None:
    from jiuwen_memory.construction.common import DEFAULT_IMPORTANCE_CATEGORY_MAP

    category_map = default_config_dict()["globals"]["importance_category_map"]
    assert set(category_map) == {
        "identity",
        "rule",
        "preference",
        "knowledge",
        "relation",
        "default",
        "transient",
    }
    assert category_map == DEFAULT_IMPORTANCE_CATEGORY_MAP, "出厂表须与代码常量一致"


def test_category_prompt_switch_defaults_off_in_globals() -> None:
    assert default_config_dict()["globals"]["importance_category_prompt_enabled"] is False


def test_default_importance_category_map_can_be_overridden() -> None:
    from jiuwen_memory.common.type_def import MemoryUnit, Modality, Segment
    from jiuwen_memory.config.context import AssemblyContext
    from jiuwen_memory.construction.bootstrap import register_constructors
    from jiuwen_memory.construction.classifier import ClassifierProducer

    register_constructors()
    raw = default_config_dict()
    # 用户 YAML 覆盖 globals 时整张表替换：未列出的 rule 回落到新表的 default。
    raw["globals"]["importance_category_map"] = {"preference": "0.60", "default": "0.40"}
    raw["classifier"]["default"] = "keyword"
    classifier = ClassifierProducer.build_named("default", AssemblyContext.from_dict(raw))
    units = classifier.classify(
        [
            MemoryUnit(id=uid, segments=[Segment(content=text, source=Modality.TEXT)])
            for uid, text in (("p", "我喜欢喝咖啡"), ("r", "必须先评审再合入"))
        ]
    )
    assert [unit.system_metadata["importance"] for unit in units] == ["0.60", "0.40"]
