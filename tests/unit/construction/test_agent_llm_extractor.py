"""AgentLlmExtractor（agent_llm target）单元测试（F09 §5）。

覆盖：
- language=cn（默认）/ en 两装配态的 prompt 面选择；非法 language 兜底 cn；
- prompt 是 user↔agent 场景重定向版（非 llm 的人际对话版）；
- 输出契约恒英文（source_id / tier / memory_class 枚举 / schema 骨架），解析管线继承 llm；
- memory_class 抽取时同步判定：合法值落 system_metadata、非法/缺失兜底 long_term_memory；
- user 侧模板双语（observation_date 前缀、context 参考段）；
- fidelity suffix 双语 opt-in；
- llm target 类属性化回归（默认实例 prompt 面 = 模块常量，行为零变化）。
"""

import json

from jiuwen_memory.common.type_def import MEMORY_CLASS_KEY, MemoryTier
from jiuwen_memory.construction.base import ExtractContext
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import (
    _EXTRACT_SYSTEM_PROMPT_CN,
    _EXTRACT_SYSTEM_PROMPT_EN,
    _EXTRACTION_FIDELITY_SUFFIX_CN,
    _EXTRACTION_FIDELITY_SUFFIX_EN,
    AgentLlmExtractor,
)
from jiuwen_memory.construction.extractor_impl.llm_extractor import (
    _CONTEXT_SECTION_TEMPLATES,
    _EXTRACT_SYSTEM_PROMPT,
    _EXTRACTION_FIDELITY_SUFFIX,
    _OBSERVATION_DATE_PREFIX,
    _PROCEDURAL_SYSTEM_PROMPT,
    ExtractorImpl,
    _format_context_block,
)
from tests.unit.construction.fixtures import MockLLM, create_test_unit


class RecordingLLM(MockLLM):
    """MockLLM 的记录版：保存每次收到的 messages，供 prompt 选择断言。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        super().__init__(responses=responses)
        self.calls: list[list] = []

    def chat(self, messages: list, **options: object) -> str:
        self.calls.append(messages)
        return super().chat(messages, **options)


def _make(
    responses: list[str] | None = None, language: str = "cn"
) -> tuple[AgentLlmExtractor, RecordingLLM]:
    llm = RecordingLLM(responses=responses)
    return AgentLlmExtractor(llm=llm, language=language), llm


def _candidate_json(
    unit_id: str = "u1", memory_class: str | None = "daily_memory"
) -> str:
    """带合法 source_id 的单条候选（走通解析与回绑）。

    memory_class=None 时省略该字段（测缺失兜底）；默认 "daily_memory"（聚餐是
    短期安排，与 prompt 里的 daily_memory 例句同款）。
    """
    item = {
        "source_id": unit_id,
        "target": "fact",
        "content": "用户计划周五晚聚餐",
        "title": "聚餐安排",
        "tags": ["聚餐"],
        "confidence": 1.0,
    }
    if memory_class is not None:
        item["memory_class"] = memory_class
    return json.dumps([item])


# ---------------------------------------------------------------------------
# T-AL-01/02/03：language 装配态选择
# ---------------------------------------------------------------------------


def test_default_language_cn():
    """默认 language=cn：主 prompt 取 CN 常量。"""
    ext, _ = _make()
    assert ext.EXTRACT_SYSTEM_PROMPT is _EXTRACT_SYSTEM_PROMPT_CN, "默认应选 CN 指令面"


def test_language_en():
    """显式 language=en：主 prompt 取 EN 常量。"""
    ext, _ = _make(language="en")
    assert ext.EXTRACT_SYSTEM_PROMPT is _EXTRACT_SYSTEM_PROMPT_EN, "显式 en 应选 EN 指令面"


def test_invalid_language_fallback_cn():
    """非法 language 兜底 cn。"""
    ext, _ = _make(language="fr")
    assert ext.EXTRACT_SYSTEM_PROMPT is _EXTRACT_SYSTEM_PROMPT_CN, "非法 language 应回落 cn"


# ---------------------------------------------------------------------------
# T-AL-04/05：场景重定向与输出契约
# ---------------------------------------------------------------------------


def test_prompts_are_agent_directed():
    """CN/EN 主 prompt 均为 user↔agent 场景重定向版。"""
    assert "这是一场用户与助手的对话" in _EXTRACT_SYSTEM_PROMPT_CN, "CN 版应声明 user↔agent 场景"
    assert "以用户为中心" in _EXTRACT_SYSTEM_PROMPT_CN, "CN 版应声明以用户为中心提取"
    assert "between a user and the assistant" in _EXTRACT_SYSTEM_PROMPT_EN, "EN 版应声明场景"
    assert "user-centred" in _EXTRACT_SYSTEM_PROMPT_EN, "EN 版应声明提取中心"


def test_en_prompt_not_reuse_baseline():
    """EN 版为新写重定向版，不复用 llm 的人际对话方向 _EXTRACT_SYSTEM_PROMPT。"""
    assert _EXTRACT_SYSTEM_PROMPT_EN is not _EXTRACT_SYSTEM_PROMPT, "EN 版应独立常量"
    assert (
        "conversation between a user and the assistant" not in _EXTRACT_SYSTEM_PROMPT
    ), "人际对话基线不应含 user↔agent 场景声明"


def test_output_contract_fields_english_in_both():
    """输出契约恒英文：两版都含 source_id / tier / memory_class 枚举 / schema 骨架。"""
    for prompt in (_EXTRACT_SYSTEM_PROMPT_CN, _EXTRACT_SYSTEM_PROMPT_EN):
        assert '"source_id"' in prompt, "契约字段名恒英文"
        assert '"episodic" | "semantic" | "procedural"' in prompt, "tier 枚举值恒英文"
        assert (
            '"user_profile" | "long_term_memory" | "daily_memory"' in prompt
        ), "memory_class 枚举值恒英文"
        assert '"event_date"' in prompt, "schema 骨架恒英文"


def test_memory_class_described_in_prompts():
    """两版 prompt 都对 memory_class 有判定描述：字段说明 + 判定顺序 + 兜底值。"""
    assert '"memory_class"' in _EXTRACT_SYSTEM_PROMPT_CN, "CN 版应含字段说明"
    assert "判定顺序" in _EXTRACT_SYSTEM_PROMPT_CN, "CN 版应含判定顺序"
    assert '拿不准时用 "long_term_memory"' in _EXTRACT_SYSTEM_PROMPT_CN, "CN 版应含兜底值"
    assert '"memory_class"' in _EXTRACT_SYSTEM_PROMPT_EN, "EN 版应含字段说明"
    assert "Order of decision" in _EXTRACT_SYSTEM_PROMPT_EN, "EN 版应含判定顺序"
    assert 'When unsure, "long_term_memory"' in _EXTRACT_SYSTEM_PROMPT_EN, "EN 版应含兜底值"


# ---------------------------------------------------------------------------
# T-AL-13/14/15：memory_class 抽取时同步判定与值域收口（F09）
# ---------------------------------------------------------------------------


def test_memory_class_lands_in_system_metadata():
    """合法 memory_class 经 build_candidates 透传落 system_metadata[MEMORY_CLASS_KEY]。"""
    ext, _ = _make(responses=[_candidate_json(memory_class="daily_memory")])
    result = ext.extract([create_test_unit("u1", "周五晚上聚餐，4个人")])

    assert len(result) == 1, "应产出 1 条派生 unit"
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "daily_memory", (
        "合法三类应原样落 system_metadata，不被改写"
    )


def test_memory_class_invalid_falls_back():
    """非法值（不在三类枚举内）兜底 long_term_memory，不透传坏值污染 md 分流。"""
    ext, _ = _make(responses=[_candidate_json(memory_class="profile")])
    result = ext.extract([create_test_unit("u1", "用户偏好书面交接")])

    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory", (
        "非法 memory_class 应兜底 long_term_memory——md 落盘分流键不能收到未知值"
    )


def test_memory_class_missing_falls_back():
    """LLM 未输出 memory_class（如 procedural 汇总路径）静默兜底 long_term_memory。"""
    ext, _ = _make(responses=[_candidate_json(memory_class=None)])
    result = ext.extract([create_test_unit("u1", "用户的项目选了杭州区域")])

    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory", (
        "缺失 memory_class 应静默兜底 long_term_memory（无 warning 噪音）"
    )


# ---------------------------------------------------------------------------
# T-AL-06：extract 继承 llm 管线（派生 unit 契约）
# ---------------------------------------------------------------------------


def test_extract_contract_inherited_cn():
    """CN 装配态走完整 extract：派生 unit 契约与 llm 一致（provenance/tier/tags）。"""
    ext, llm = _make(responses=[_candidate_json()])
    result = ext.extract([create_test_unit("u1", "周五晚上聚餐，4个人")])

    assert len(result) == 1, "应产出 1 条派生 unit"
    derived = result[0]
    assert derived.provenance == ["u1"], "provenance 回指源 unit"
    assert derived.source_ref == "u1", "source_ref 回指源 unit"
    assert derived.tier == MemoryTier.SEMANTIC, "无 tier 候选兜底 SEMANTIC"
    assert "extracted" in derived.tags, "tags 应含 extracted 派生标记"
    assert "聚餐" in derived.tags, "LLM 主题 tags 应并入"
    sys_msg = llm.calls[0][0]
    assert sys_msg.role == "system", "messages[0] 应为 system"
    assert sys_msg.content is _EXTRACT_SYSTEM_PROMPT_CN, "CN 装配态应发送 CN system prompt"


# ---------------------------------------------------------------------------
# T-AL-07/08：user 侧模板双语
# ---------------------------------------------------------------------------


def test_user_prompt_bilingual_cn():
    """CN 态 user prompt：observation_date 前缀与 context 参考段为中文模板。"""
    ext, llm = _make(responses=[_candidate_json()])
    ctx = ExtractContext(
        recent_originals=[create_test_unit("m1", "上次聊到聚餐")],
        related_memories=[create_test_unit("m2", "用户喜欢川菜")],
    )
    ext.extract([create_test_unit("u1", "就定周五")], context=ctx)

    user = llm.calls[0][1]
    assert user.role == "user", "messages[1] 应为 user"
    assert user.content.startswith("observation_date: "), "user prompt 应以基准日期行开头"
    assert "以该日期为基准" in user.content, "CN 态相对时间说明应为中文"
    assert "## 近期对话上下文" in user.content, "CN 态 context 段标题应为中文"
    assert "## 已有相关记忆" in user.content, "CN 态 related 段标题应为中文"
    assert "- 上次聊到聚餐" in user.content, "recent 原文应逐条列出"
    assert "- 用户喜欢川菜" in user.content, "related 记忆应逐条列出"


def test_user_prompt_bilingual_en():
    """EN 态 user prompt：observation_date 前缀与 context 参考段为英文模板。"""
    ext, llm = _make(responses=[_candidate_json()], language="en")
    ctx = ExtractContext(
        recent_originals=[create_test_unit("m1", "上次聊到聚餐")],
        related_memories=[],
    )
    ext.extract([create_test_unit("u1", "就定周五")], context=ctx)

    user = llm.calls[0][1]
    assert "Use this as the reference point" in user.content, "EN 态相对时间说明应为英文"
    assert "## Recent conversation context" in user.content, "EN 态 context 段标题应为英文"


# ---------------------------------------------------------------------------
# T-AL-09：fidelity suffix 双语 opt-in
# ---------------------------------------------------------------------------


def test_fidelity_suffix_bilingual():
    """extraction_fidelity_mode=true 时 system prompt 追加对应语言的保真段。"""
    ext_cn, llm_cn = _make(responses=[_candidate_json()])
    u_cn = create_test_unit("u1", "就定周五")
    u_cn.system_metadata = {"extraction_fidelity_mode": "true"}
    ext_cn.extract([u_cn])
    assert llm_cn.calls[0][0].content == (
        _EXTRACT_SYSTEM_PROMPT_CN + "\n\n" + _EXTRACTION_FIDELITY_SUFFIX_CN
    ), "CN 态 fidelity 追加段应为中文"

    ext_en, llm_en = _make(responses=[_candidate_json()], language="en")
    u_en = create_test_unit("u1", "就定周五")
    u_en.system_metadata = {"extraction_fidelity_mode": "true"}
    ext_en.extract([u_en])
    assert llm_en.calls[0][0].content.endswith(_EXTRACTION_FIDELITY_SUFFIX_EN), (
        "EN 态 fidelity 追加段应为英文"
    )


# ---------------------------------------------------------------------------
# T-AL-10/11/12：llm target 类属性化回归 + 注册
# ---------------------------------------------------------------------------


def test_llm_target_prompt_face_unchanged():
    """回归：类属性化后 llm 默认实例的 prompt 面 = 模块常量（行为零变化）。"""
    ext = ExtractorImpl(llm=MockLLM())
    assert ext.EXTRACT_SYSTEM_PROMPT is _EXTRACT_SYSTEM_PROMPT, "主 prompt 默认值不变"
    assert ext.PROCEDURAL_SYSTEM_PROMPT is _PROCEDURAL_SYSTEM_PROMPT, "procedural 默认值不变"
    assert ext.EXTRACTION_FIDELITY_SUFFIX is _EXTRACTION_FIDELITY_SUFFIX, "保真段默认值不变"
    assert ext.OBSERVATION_DATE_PREFIX is _OBSERVATION_DATE_PREFIX, "日期前缀默认值不变"
    assert ext.CONTEXT_SECTION_TEMPLATES is _CONTEXT_SECTION_TEMPLATES, "context 模板默认值不变"


def test_format_context_block_default_english():
    """回归：_format_context_block 不传 templates 时输出英文标题（外部消费者零改动）。"""
    ctx = ExtractContext(
        recent_originals=[create_test_unit("m1", "上文")],
        related_memories=[create_test_unit("m2", "已有")],
    )
    block = _format_context_block(ctx)
    assert "## Recent conversation context" in block, "默认 context 段标题应保持英文"
    assert "## Existing related memories" in block, "默认 related 段标题应保持英文"
    assert "- 上文" in block and "- 已有" in block, "条目行应逐条列出"


def test_agent_llm_registered():
    """agent_llm target 经 import 自注册进 ExtractorProducer。"""
    assert "agent_llm" in ExtractorProducer.known(), "agent_llm 应出现在注册表"
