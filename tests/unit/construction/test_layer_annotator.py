"""LayerAnnotator 单元测试。

覆盖 keyword / llm 两实现：
- 超阈 content 标注 L0/L1、短 content 留空（阈值筛选）；
- keyword 版规则（L1 前 N 字、L0 首句/content）；
- llm 版（MockLLM）批量回填、失败降级空；
- best effort：失败不阻断。
"""

import json

import pytest

from common.type_def import MemoryUnit
from construction.layer_annotator_impl.keyword_layer_annotator import KeywordLayerAnnotator
from construction.layer_annotator_impl.llm_layer_annotator import LLMLayerAnnotator
from tests.unit.construction.fixtures import (
    MockLLM,
    create_test_unit,
)

# 用于构造超阈 content 的长文本（> 512 字符）。
_LONG_CONTENT = (
    "Alice 是 BlinkMem 项目的后端负责人，擅长 Python 和 Go，负责整体架构设计与核心模块开发。"
    "她每周三主持技术评审会，习惯用 pytest 写自动化测试，并要求团队遵循代码审查规范。"
    "最近一周她在研究向量数据库的 ANN 算法，打算把召回延迟降到 50ms 以内，"
    "为此对比了 HNSW 与 IVF-PQ。"
    "她对咖啡的偏好是早上喝美式、下午喝拿铁，且不加糖，认为这能保持下午的专注力。"
    "在团队管理上她主张文档先行，所有接口变更必须先更新设计文档再动代码。"
    "她还负责新人的 mentor 工作，每月组织一次技术分享会，主题由成员轮流申报。"
    "技术栈方面，后端用 FastAPI 加 SQLAlchemy，前端用 React 加 TypeScript，部署走 Docker Compose。"
    "她坚持 CI/CD 流水线必须覆盖单元测试、集成测试与安全扫描三个环节，缺一不可。"
    "对于数据库设计，她偏好先做领域模型再落表结构，反对直接拿 ER 图反推。"
    "在沟通上她要求会议有议程、有纪要、有 follow-up，反对无结论的讨论。"
    "关于性能监控，她要求所有核心接口接入 Prometheus 指标采集，并配置 Grafana 告警面板。"
    "她认为可观测性是生产系统的底线，没有监控的服务不允许上线。"
)


def _long_unit(uid: str = "u1") -> MemoryUnit:
    """构造超阈 content 的 unit。"""
    return create_test_unit(uid, _LONG_CONTENT)


# ---------------------------------------------------------------------------
# KeywordLayerAnnotator
# ---------------------------------------------------------------------------


def test_keyword_annotate_long_content():
    """超阈 content → L0/L1 非空（规则版）。"""
    ann = KeywordLayerAnnotator(layers_threshold=50)
    unit = _long_unit()
    ann.annotate([unit])

    assert unit.layers.l1  # 前 N 字
    assert unit.layers.l0  # tags + 首句
    assert unit.layers.l1 == _LONG_CONTENT[: 200]


def test_keyword_annotate_short_content_skipped():
    """短 content → layers 留空（阈值筛选）。"""
    ann = KeywordLayerAnnotator(layers_threshold=50)
    unit = create_test_unit("u1", "短内容")
    ann.annotate([unit])

    assert unit.layers.l0 == ""
    assert unit.layers.l1 == ""


def test_keyword_annotate_short_content_l0_equals_content():
    """content ≤ 100 字时 L0 = content。"""
    ann = KeywordLayerAnnotator(layers_threshold=50)
    # 50 字 < 100，超阈(>50) 但 ≤100 → L0 = content
    unit = create_test_unit("u1", "x" * 60)
    ann.annotate([unit])

    assert unit.layers.l0 == "x" * 60


def test_keyword_annotate_threshold_configurable():
    """阈值可调：调低后短 content 也触发。"""
    ann = KeywordLayerAnnotator(layers_threshold=5)
    unit = create_test_unit("u1", "短内容但超阈5")
    ann.annotate([unit])

    assert unit.layers.l0  # 触发了


def test_keyword_first_sentence_mixed_locale():
    """first_sentence 取所有分隔符里最早位置，而非按类型顺序首个命中。

    回归评审 bug："Hello. 这是中文。" 应截到位置5的英文句号 "Hello."，
    而非按（。→！→？→.）顺序首个命中的中文句号位置。
    """
    # 中英混合：英文句号 . 位置5 早于中文句号 。 位置13
    s = KeywordLayerAnnotator.first_sentence("Hello. 这是中文。")
    assert s == "Hello.", f"应截到最早出现的英文句号，实际 {s!r}"

    # 纯中文：第一个中文句号
    assert KeywordLayerAnnotator.first_sentence("第一句。第二句。") == "第一句。"

    # 纯英文：第一个英文句号
    assert KeywordLayerAnnotator.first_sentence("First. Second.") == "First."

    # 换行最早
    assert KeywordLayerAnnotator.first_sentence("第一行\n第二行。") == "第一行"

    # 无分隔符：截前 80 字
    assert KeywordLayerAnnotator.first_sentence("无分隔符的短文本") == "无分隔符的短文本"


# ---------------------------------------------------------------------------
# LLMLayerAnnotator
# ---------------------------------------------------------------------------


def _llm_annotator(responses: list[str]) -> LLMLayerAnnotator:
    return LLMLayerAnnotator(
        llm=MockLLM(responses=responses),
        layers_threshold=50,
        retry_max_retries=3,
        retry_backoff_ms=1000,
    )


def test_llm_annotate_long_content():
    """超阈 content → LLM 批量回填 L0/L1。"""
    ann = _llm_annotator([
        json.dumps([{"id": 0, "l0": "概要文本", "l1": "overview 文本"}])
    ])
    unit = _long_unit()
    ann.annotate([unit])

    assert unit.layers.l0 == "概要文本"
    assert unit.layers.l1 == "overview 文本"


def test_llm_annotate_short_content_skipped():
    """短 content → 不调 LLM，layers 留空。"""
    ann = _llm_annotator([])  # 无 LLM 响应，若调了会报错
    unit = create_test_unit("u1", "短")
    ann.annotate([unit])  # 不抛异常

    assert unit.layers.l0 == ""
    assert unit.layers.l1 == ""


def test_llm_annotate_failure_leaves_empty():
    """LLM 返回非 JSON → layers 留空，不阻断（best effort）。"""
    ann = _llm_annotator(["not a json"])
    unit = _long_unit()
    ann.annotate([unit])  # 不抛异常

    assert unit.layers.l0 == ""
    assert unit.layers.l1 == ""


def test_llm_annotate_batch_multiple():
    """多条超阈候选 → 一次 LLM 调用按 id 回填各条。"""
    ann = _llm_annotator([
        json.dumps([
            {"id": 0, "l0": "概要0", "l1": "overview0"},
            {"id": 1, "l0": "概要1", "l1": "overview1"},
        ])
    ])
    u0, u1 = _long_unit("u0"), _long_unit("u1")
    ann.annotate([u0, u1])

    assert u0.layers.l0 == "概要0"
    assert u1.layers.l0 == "概要1"


def test_llm_annotate_mixed_long_short():
    """长短混合 → 只对超阈的调 LLM，短的留空。"""
    ann = _llm_annotator([
        json.dumps([{"id": 0, "l0": "概要", "l1": "overview"}])
    ])
    long_u = _long_unit("u0")
    short_u = create_test_unit("u1", "短")
    ann.annotate([long_u, short_u])

    assert long_u.layers.l0 == "概要"
    assert short_u.layers.l0 == ""


def test_llm_duplicate_ids_do_not_partially_mutate_batch():
    ann = _llm_annotator([
        json.dumps([
            {"id": 0, "l0": "summary", "l1": "detailed overview"},
            {"id": 0, "l0": "other", "l1": "other detailed overview"},
        ])
    ])
    u0, u1 = _long_unit("u0"), _long_unit("u1")

    ann.annotate([u0, u1])

    assert u0.layers.l0 == ""
    assert u1.layers.l0 == ""


@pytest.mark.parametrize("invalid_id", [True, 0.9])
def test_llm_rejects_non_integer_ids(invalid_id):
    ann = _llm_annotator([
        json.dumps([{"id": invalid_id, "l0": "summary", "l1": "detailed overview"}])
    ])
    unit = _long_unit()

    ann.annotate([unit])

    assert unit.layers.l0 == ""
    assert unit.layers.l1 == ""


def test_llm_missing_id_does_not_partially_mutate_batch():
    ann = _llm_annotator([
        json.dumps([{"id": 0, "l0": "summary", "l1": "detailed overview"}])
    ])
    u0, u1 = _long_unit("u0"), _long_unit("u1")

    ann.annotate([u0, u1])

    assert u0.layers.l0 == ""
    assert u1.layers.l0 == ""


def test_llm_non_monotonic_layers_are_rejected():
    ann = _llm_annotator([
        json.dumps([{"id": 0, "l0": "long summary", "l1": "short"}])
    ])
    unit = _long_unit()

    ann.annotate([unit])

    assert unit.layers.l0 == ""
    assert unit.layers.l1 == ""


def test_llm_invalid_length_skips_only_that_item():
    """单条长度不合法不应抹掉同批其他合法分层。"""
    ann = _llm_annotator([
        json.dumps([
            {"id": 0, "l0": "long summary", "l1": "short"},
            {"id": 1, "l0": "summary", "l1": "a valid detailed overview"},
        ])
    ])
    invalid, valid = _long_unit("u0"), _long_unit("u1")

    ann.annotate([invalid, valid])

    assert invalid.layers.l0 == ""
    assert invalid.layers.l1 == ""
    assert valid.layers.l0 == "summary"
    assert valid.layers.l1 == "a valid detailed overview"
