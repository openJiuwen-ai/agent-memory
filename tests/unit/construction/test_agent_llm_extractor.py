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

import pytest

from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.type_def import MEMORY_CLASS_KEY, MemoryTier
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.construction.base import ExtractContext
from jiuwen_memory.construction.common import importance_category_prompt
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import (
    _EXTRACT_SYSTEM_PROMPT_CN,
    _EXTRACT_SYSTEM_PROMPT_EN,
    _EXTRACTION_FIDELITY_SUFFIX_CN,
    _EXTRACTION_FIDELITY_SUFFIX_EN,
    AgentLlmExtractor,
)
from jiuwen_memory.construction.extractor_impl.document_fact_extractor import DocumentFactExtractor
from jiuwen_memory.construction.extractor_impl.llm_extractor import (
    _CONTEXT_SECTION_TEMPLATES,
    _EXTRACT_SYSTEM_PROMPT,
    _EXTRACTION_FIDELITY_SUFFIX,
    _OBSERVATION_DATE_PREFIX,
    _PROCEDURAL_SYSTEM_PROMPT,
    ExtractorImpl,
    _format_context_block,
)
from jiuwen_memory.construction.extractor_impl.team_llm_extractor import TeamLlmExtractor
from tests.unit.construction.fixtures import MockLLM, create_test_unit


class RecordingLLM(MockLLM):
    """MockLLM 的记录版：保存每次收到的 messages，供 prompt 选择断言。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        super().__init__(responses=responses)
        self.calls: list[list] = []

    def chat(self, messages: list, **options: object) -> str:
        self.calls.append(messages)
        return super().chat(messages, **options)


class FailingLLM(RecordingLLM):
    """每次调用都失败的 LLM：记录调用后抛错，供总尝试次数断言。"""

    def chat(self, messages: list, **options: object) -> str:
        self.calls.append(messages)
        raise RuntimeError("down")


def _make(
    responses: list[str] | None = None, language: str = "cn",
    extractor_type: type[AgentLlmExtractor] = AgentLlmExtractor,
) -> tuple[AgentLlmExtractor, RecordingLLM]:
    llm = RecordingLLM(responses=responses)
    extractor = extractor_type(
        llm=llm, language=language, importance_category_prompt_enabled=True
    )
    return extractor, llm


def _candidate_json(
    unit_id: str = "u1", memory_class: str | None = "daily_memory", tier: str | None = None
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
    if tier is not None:
        item["tier"] = tier
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
# 可复用标识另抽稳定事实（prompt 规则）+ user_profile 层级收口
# ---------------------------------------------------------------------------


def test_reusable_identifier_rule_in_prompts():
    """两版 prompt 都含可复用标识三层分流与凭据隔离规则。"""
    cn, en = _EXTRACT_SYSTEM_PROMPT_CN, _EXTRACT_SYSTEM_PROMPT_EN
    assert "可复用标识" in cn, "CN 版应含可复用标识规则"
    assert "另为它输出一条不带日期的稳定事实" in cn, "CN 版应要求另出稳定事实"
    assert '"memory_class" 用 "user_profile"' in cn, "CN 版应写明本人常用非凭据标识归 user_profile"
    assert (
        '"memory_class" 用 "long_term_memory"' in cn
    ), "CN 版应写明首次外部标识归 long_term_memory"
    assert '"is_credential" 用 true' in cn, "CN 版应要求凭据标 is_credential=true"
    assert '绝不写进 "user_profile"' in cn, "CN 版应禁止凭据写进 user_profile"
    assert "在回答助手提问时给出的凭据" in cn, "CN 版应覆盖回答提问的情形"
    assert "不要保存或不要记住，则跳过不记录" in cn, "CN 版应尊重用户明确要求不保存"
    assert "不要附加“结合上下文”" in cn, "CN 版应禁止省略句补全附带元注释"
    assert "为复用而提供" not in cn, "CN 版不应给授权码附加「为复用而提供」的条件"
    assert "Reusable identifiers" in en, "EN 版应含可复用标识规则"
    assert "also output one undated stable fact" in en, "EN 版应要求另出稳定事实"
    assert '"memory_class" is "user_profile"' in en, "EN 版应写明本人常用非凭据标识归 user_profile"
    assert (
        'set "memory_class" to "long_term_memory"' in en
    ), "EN 版应写明首次外部标识与凭据归 long_term_memory"
    assert '"is_credential" to true' in en, "EN 版应要求凭据标 is_credential=true"
    assert 'never\n"user_profile"' in en, "EN 版应禁止凭据写进 user_profile"
    assert "answering the assistant's question" in en, "EN 版应覆盖回答提问的情形"
    assert "explicitly asks not to save or remember it" in en, "EN 版应尊重用户明确要求不保存"
    assert "without\nmeta-commentary" in en, "EN 版应禁止省略句补全附带元注释"
    assert "过后即失效的取值本身不算可复用标识" in cn, "CN 版应排除一次性取值"
    assert "SMS verification codes" in en, "EN 版应排除一次性取值"
    for prompt in (cn, en):
        assert '"project_fact": true | false' in prompt
        assert '"is_credential": true | false' in prompt
        assert "/work/paper/final" in prompt


@pytest.mark.parametrize("raw_flag", [True, "true", "TRUE", " True "])
def test_credential_is_coerced_to_long_term_memory_and_keeps_source_project(raw_flag):
    """凭据（布尔或字符串 true）统一收口到 long_term_memory，保留来源 project 与 !472 查侧对齐。"""
    item = json.loads(_candidate_json(memory_class="user_profile"))[0]
    item.update(
        content="用户邮箱 ops@example.com 的授权码是 abcd1234efgh",
        project_fact=False,
        is_credential=raw_flag,
    )
    ext, _ = _make(responses=[json.dumps([item])])
    source = create_test_unit("u1", "授权码是 abcd1234efgh")
    source.system_metadata["project"] = "default"

    result = ext.extract([source])

    assert len(result) == 1
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert result[0].system_metadata["is_credential"] == "true"
    assert result[0].system_metadata["project"] == "default"


def test_credential_in_daily_memory_is_also_coerced_to_long_term_memory():
    """模型把凭据误标成 daily_memory 时，同样强制收口到 long_term_memory。"""
    item = json.loads(_candidate_json(memory_class="daily_memory"))[0]
    item.update(
        content="用户邮箱 ops@example.com 的授权码是 abcd1234efgh",
        project_fact=False,
        is_credential=True,
    )
    ext, _ = _make(responses=[json.dumps([item])])

    result = ext.extract([create_test_unit("u1", "授权码是 abcd1234efgh")])

    assert len(result) == 1
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert result[0].system_metadata["is_credential"] == "true"


def test_non_credential_profile_keeps_user_profile_and_clears_project():
    """is_credential=False 的用户画像保留 user_profile 且清空来源项目坐标（含 coords.project）。"""
    item = json.loads(_candidate_json(memory_class="user_profile"))[0]
    item.update(
        content="用户常用发件邮箱是 ops@example.com，收件邮箱是 lead@example.org",
        project_fact=False,
        is_credential=False,
    )
    ext, _ = _make(responses=[json.dumps([item])])
    source = create_test_unit("u1", "我平时用 ops@example.com 给 lead@example.org 发周报")
    source.system_metadata["project"] = "proj-a"
    source.system_metadata["coords"] = {"project": "proj-a", "team": "core"}

    result = ext.extract([source])

    assert len(result) == 1
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "user_profile"
    assert result[0].system_metadata["project"] == ""
    assert "project" not in result[0].system_metadata.get("coords", {})
    assert result[0].system_metadata["coords"]["team"] == "core"
    assert "is_credential" not in result[0].system_metadata


def test_first_time_external_identifier_keeps_source_project():
    """首次外部标识（long_term_memory）保留来源 project，避免与 Work project==X 查侧错配。"""
    item = json.loads(_candidate_json(memory_class="long_term_memory"))[0]
    item.update(
        content="用户为这次测试文件发送指定了收件邮箱 dest@example.org。",
        project_fact=False,
        is_credential=False,
    )
    ext, _ = _make(responses=[json.dumps([item])])
    source = create_test_unit("u1", "把 /tmp/test.txt 发给 dest@example.org")
    source.system_metadata["project"] = "default"

    result = ext.extract([source])

    assert len(result) == 1
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert result[0].system_metadata["project"] == "default"
    assert "is_credential" not in result[0].system_metadata


def test_extractor_handles_invalid_class_and_coerces_episodic_credential_to_semantic():
    """验证抽取边界：非法 memory_class 兜底 long_term_memory、凭据 episodic 收口 semantic。"""
    cred_item = json.loads(_candidate_json(memory_class="daily_memory", tier="episodic"))[0]
    cred_item.update(
        content="用户邮箱 ops@example.com 的授权码是 abcd1234",
        is_credential=True,
    )
    fallback_class_item = json.loads(_candidate_json(memory_class="invalid_cls"))[0]
    fallback_class_item.update(
        content="用户为测试发送指定了收件邮箱 dest@example.org",
        project_fact="false",
    )
    ext, _ = _make(responses=[json.dumps([cred_item, fallback_class_item])])
    source = create_test_unit("u1", "授权码与收件邮箱")
    source.system_metadata["project"] = "default"

    result = ext.extract([source])

    assert len(result) == 2
    cred_unit, fallback_unit = result
    assert cred_unit.tier is MemoryTier.SEMANTIC, "凭据标成 episodic 应收口为 semantic"
    assert cred_unit.system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert cred_unit.system_metadata["is_credential"] == "true"
    assert cred_unit.system_metadata["project"] == "default"

    assert fallback_unit.system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert fallback_unit.system_metadata["project"] == "default"


def test_source_is_credential_not_propagated_to_non_credential_profile():
    """源单元自带 is_credential=true 时，不得经继承污染非凭据 user_profile 派生记忆。"""
    item = json.loads(_candidate_json(memory_class="user_profile"))[0]
    item.update(
        content="用户常用发件邮箱是 ops@example.com",
        project_fact=False,
        is_credential=False,
    )
    ext, _ = _make(responses=[json.dumps([item])])
    source = create_test_unit("u1", "我的常用发件邮箱是 ops@example.com")
    source.system_metadata.update(infer="true", is_credential="true")

    result = ext.extract([source])

    assert len(result) == 1
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "user_profile"
    assert "is_credential" not in result[0].system_metadata


def test_non_reclassifying_extractors_preserve_source_is_credential():
    """不做凭据重判的抽取路径（Keyword/ExtractorImpl/Schema）保留源凭据标记。"""
    from jiuwen_memory.common.chunker.chunker_impl.fixed_window_chunker import FixedWindowChunker
    from jiuwen_memory.construction.extractor_impl.entity_schema_extractor import (
        _inherited_schema_system_metadata,
    )
    from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor

    cred_source = create_test_unit("u-cred", "SMTP 授权码是 secret-token-xyz")
    cred_source.system_metadata.update(infer="true", is_credential="true")
    normal_source = create_test_unit("u-norm", "普通记录")
    normal_source.system_metadata.update(infer="true")

    kw = KeywordExtractor(chunker=FixedWindowChunker(size=200))
    kw_derived = kw.extract([cred_source])
    assert len(kw_derived) == 1
    assert kw_derived[0].system_metadata.get("is_credential") == "true"

    proc_cred = create_test_unit("u-proc-cred", "步骤 1：使用密钥 secret-token-xyz")
    proc_cred.system_metadata.update(procedural="true", is_credential="true")
    kw_proc = kw.extract([proc_cred, normal_source])
    assert len(kw_proc) == 1
    assert kw_proc[0].system_metadata.get("is_credential") == "true"

    base_item = json.loads(_candidate_json())[0]
    base_item["source_id"] = "u-cred"
    base_ext = ExtractorImpl(llm=RecordingLLM(responses=[json.dumps([base_item])]))
    base_derived = base_ext.extract([cred_source])
    assert len(base_derived) == 1
    assert base_derived[0].system_metadata.get("is_credential") == "true"

    proc_ext, _ = _make(
        responses=[json.dumps({"content": "使用凭证完成操作", "title": "凭证流程"})],
        extractor_type=AgentLlmExtractor,
    )
    proc_derived = proc_ext.extract([proc_cred, normal_source])
    assert len(proc_derived) == 1
    assert proc_derived[0].system_metadata.get("is_credential") == "true"

    schema_meta = _inherited_schema_system_metadata([normal_source, cred_source])
    assert schema_meta.get("is_credential") == "true"


@pytest.mark.parametrize("project", ["", "   ", "KVCache"])
@pytest.mark.parametrize("app", ["jiuwenswarm", "another_agent", ""])
def test_project_fact_uses_only_source_project(project, app):
    """同一轮里，项目终稿路径受项目约束，个人邮箱仍按画像处理。"""
    project_item = json.loads(_candidate_json(memory_class=" user_profile "))[0]
    project_item.update(
        content="项目的论文终稿放在 /work/paper/final",
        project_fact=True,
        project="model-supplied-project",
    )
    profile_item = json.loads(_candidate_json(memory_class="user_profile"))[0]
    profile_item.update(content="用户常用邮箱是 user@example.org", project_fact=False)
    unclassified_item = {**project_item, "content": "另一个项目的部署地址"}
    del unclassified_item["project_fact"]
    ext, _ = _make(
        responses=[json.dumps([project_item, unclassified_item, profile_item])],
        extractor_type=DocumentFactExtractor,
    )
    source = create_test_unit("u1", "终稿放在 /work/paper/final，我的邮箱是 user@example.org")
    source.system_metadata.update(source_app=app, project=project)

    result = ext.extract([source])

    profile = result[-1]
    assert profile.content == profile_item["content"]
    assert profile.system_metadata[MEMORY_CLASS_KEY] == "user_profile"
    if not project.strip():
        assert len(result) == 1, "无项目时应跳过项目候选"
        return
    assert len(result) == 2
    assert result[0].content == project_item["content"]
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert result[0].system_metadata["project"] == project


def test_user_profile_episodic_is_coerced_to_semantic():
    """user_profile 条目被模型标成 episodic 时收口为 semantic（用户画像是稳定属性）。"""
    ext, _ = _make(responses=[_candidate_json(memory_class="user_profile", tier="episodic")])
    result = ext.extract([create_test_unit("u1", "用 a@example.com 发给 b@example.org")])

    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "user_profile", "归属类别不应被改写"
    assert result[0].tier is MemoryTier.SEMANTIC, "user_profile + episodic 应收口为 semantic"


def test_user_profile_procedural_is_kept():
    """user_profile + procedural 不动：用户的习惯可以属于 procedural，同为稳定类。"""
    ext, _ = _make(responses=[_candidate_json(memory_class="user_profile", tier="procedural")])
    result = ext.extract([create_test_unit("u1", "用户发布前总是先跑测试")])

    assert result[0].tier is MemoryTier.PROCEDURAL, "user_profile + procedural 不应被改写"


def test_non_profile_episodic_is_kept():
    """层级收口只针对 user_profile：daily_memory 的 episodic 事件保持不变。"""
    ext, _ = _make(responses=[_candidate_json(memory_class="daily_memory", tier="episodic")])
    result = ext.extract([create_test_unit("u1", "周五晚上聚餐，4个人")])

    assert result[0].tier is MemoryTier.EPISODIC, "非 user_profile 的 episodic 不应被改写"


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
    assert sys_msg.content == (
        _EXTRACT_SYSTEM_PROMPT_CN + "\n" + importance_category_prompt(None, language="cn")
    ), "CN 装配态应发送 CN system prompt（末尾追加中文重要度类别段）"


@pytest.mark.parametrize(
    ("language", "baseline"),
    [
        ("cn", _EXTRACT_SYSTEM_PROMPT_CN),
        ("en", _EXTRACT_SYSTEM_PROMPT_EN),
    ],
)
def test_category_prompt_off_matches_baseline(language: str, baseline: str) -> None:
    llm = RecordingLLM(responses=[_candidate_json()])
    AgentLlmExtractor(llm=llm, language=language).extract([create_test_unit("u1", "就定周五")])
    content = llm.calls[0][0].content
    assert content == baseline, "关闭重要度类别时应逐字发送对应语言的基础提示词"


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
        _EXTRACT_SYSTEM_PROMPT_CN
        + "\n"
        + importance_category_prompt(None, language="cn")
        + "\n\n"
        + _EXTRACTION_FIDELITY_SUFFIX_CN
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


def test_agent_llm_factory_reads_new_attempt_keys():
    """工厂读新键 extractor_max_attempts：LLM 持续失败时总调用次数等于配置值，不回落默认 3。"""
    ctx = AssemblyContext()
    llm = FailingLLM()
    LlmProducer.put("failing", llm, ctx=ctx)
    extractor = ExtractorProducer.build(
        "agent_llm",
        {"llm": "failing", "extractor_max_attempts": 2, "extractor_retry_backoff": 0},
        ctx,
    )
    assert isinstance(extractor, AgentLlmExtractor), "agent_llm 应装配出 AgentLlmExtractor"

    with pytest.raises(RuntimeError, match="down"):
        extractor.extract([create_test_unit("u1", "周五晚上聚餐，4个人")])

    assert len(llm.calls) == 2, "extractor_max_attempts=2 应只调用 LLM 两次"


def test_generic_extractor_does_not_select_policy_from_app_metadata():
    extractor, _ = _make(responses=[_candidate_json()])
    source = create_test_unit("u1", "durable fact")
    source.system_metadata.update(source_app="jiuwenswarm", memory_scene="team")
    assert "team_internal" not in extractor.extraction_system_prompt([source])
    assert len(extractor.extract([source])) == 1


@pytest.mark.unit
def test_team_extractor_uses_authorized_destinations_in_one_model_request():
    team_responsibility = "Ada is responsible for reviewing this team's schema changes."
    user_preference = "I prefer written handoffs."
    agent_method = "Validate column defaults before applying a migration."
    source = create_test_unit("source", f"{team_responsibility} {user_preference}")
    source.system_metadata["source_role"] = "user"
    assistant_guess = "The user prefers voice handoffs."
    assistant = create_test_unit("assistant", f"{assistant_guess} {agent_method}")
    assistant.system_metadata["source_role"] = "assistant"
    payload = [
        {"content": team_responsibility, "source_id": "source", "evidence": team_responsibility,
         "kind": "user_fact", "destination": "team", "label": "member"},
        {"content": "Generated preference", "source_id": "source", "evidence": user_preference,
         "kind": "user_fact", "destination": "user_profile"},
        {"content": assistant_guess, "source_id": "assistant", "evidence": assistant_guess,
         "kind": "user_fact", "destination": "user_profile"},
        {"content": agent_method, "source_id": "assistant", "evidence": agent_method,
         "kind": "experience", "destination": "agent", "label": "lesson"},
        {"content": agent_method, "source_id": "assistant", "evidence": agent_method,
         "kind": "experience", "destination": "personal", "label": "lesson"},
    ]
    extractor, model = _make([json.dumps(payload)], extractor_type=TeamLlmExtractor)
    units = extractor.extract([source, assistant], context=ExtractContext(collective_destinations={
        "team": "team-id", "user_profile": "owner", "agent": "agent-id",
    }))
    assert [
        (unit.content, unit.system_metadata["collective_destination"],
         unit.system_metadata.get("team_label"))
        for unit in units
    ] == [
        (team_responsibility, "team", "member"),
        (user_preference, "user_profile", None),
        (agent_method, "agent", "lesson"),
    ]
    assert len(model.calls) == 1
    instructions = model.calls[0][0].content
    assert "one candidate per independent item" in instructions
    assert "exclude unrelated claims from the quote" in instructions
    assert "Select at most one label" in instructions
    for label in ("decision", "lesson", "member", "context"):
        assert f"Use {label} for" in instructions
    request = json.loads(model.calls[0][-1].content)
    assert set(request["destinations"]) == {"team", "user_profile", "agent"}
    assert set(request["destination_descriptions"]) == {"team", "user_profile", "agent"}


@pytest.mark.unit
def test_team_extractor_rejects_agent_destination_without_agent_landing():
    content = "Validate column defaults before applying a migration."
    source = create_test_unit("source", content)
    source.system_metadata["source_role"] = "assistant"
    payload = [{
        "content": content, "source_id": "source", "evidence": content,
        "kind": "experience", "destination": "agent",
    }]
    extractor, model = _make([json.dumps(payload)], extractor_type=TeamLlmExtractor)
    context = ExtractContext(collective_destinations={"team": "team-id"})

    assert extractor.extract([source], context=context) == []
    assert json.loads(model.calls[0][-1].content)["destinations"] == ["team"]
