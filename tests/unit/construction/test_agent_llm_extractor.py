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
from unittest.mock import Mock, patch

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
    """两版 prompt 均含可复用标识规则：首次/单次任务另出稳定事实，排除一次性值不连带跳过邮箱。"""
    cn, en = _EXTRACT_SYSTEM_PROMPT_CN, " ".join(_EXTRACT_SYSTEM_PROMPT_EN.split())
    assert "可复用标识" in cn, "CN 版应含可复用标识规则"
    assert "另为它输出一条不带日期的稳定事实" in cn, "CN 版应要求另出稳定事实"
    assert '"memory_class" 一律为 "user_profile"' in cn, "CN 版应写明非凭据标识一律归 user_profile"
    assert (
        '写明它对应的账号或服务，但 "memory_class" 用 "long_term_memory"、"is_credential" 用 true、'
        '跨项目时 "project_fact" 用 false（不写进 "user_profile"）' in cn
    ), "CN 版应要求凭据关联账号/服务、归 long_term_memory 且标 is_credential=true，不写进 USER.md"
    assert "用户在回答助手提问时给出的标识同样适用" in cn, "CN 版应覆盖回答提问的情形"
    assert "为复用而提供" not in cn, "CN 版不应给授权码附加「为复用而提供」的条件"
    assert "无论是不是首次出现、无论当前是不是测试或单次任务" in cn, (
        "CN 版应覆盖首次出现及测试/单次任务"
    )
    assert "常用收件人" not in cn, "CN 版不应以「常用收件人」限制首次收件邮箱抽取"
    assert "发件邮箱" in cn and "收件邮箱" in cn, "CN 版应明确覆盖发件邮箱与收件邮箱"
    assert "/tmp/test.txt" in cn, "CN 版应含单次测试文件 + 邮箱示例"
    assert "sender@example.com" in cn and "recipient@example.org" in cn, (
        "CN 版示例应使用 RFC 2606 保留示例域名"
    )
    assert "测试文件发送指定了发件邮箱" in cn and "不要把单次测试泛化成通用默认收件人" in cn, (
        "CN 版应要求保留单次测试用途，不泛化为默认收件人且不把请求写成已发送"
    )
    assert "若该账号、服务器或地址仅属于某个特定项目" in cn, (
        "CN 版应明确项目专用账号/地址让位给项目事实规则"
    )
    assert "过后即失效的取值本身不算可复用标识" in cn, "CN 版应排除一次性取值本身"
    assert "绝不要因此跳过同一句话里同时给出的邮箱" in cn, (
        "CN 版应防止临时文件名抑制同句邮箱提取"
    )
    assert "Reusable identifiers" in en, "EN 版应含可复用标识规则"
    assert "also output one undated stable fact" in en, "EN 版应要求另出稳定事实"
    assert '"memory_class" is always "user_profile"' in en, (
        "EN 版应写明非凭据标识一律归 user_profile"
    )
    assert (
        'naming the account or service it belongs to, but set "memory_class" to '
        '"long_term_memory", "is_credential" to true, and "project_fact" to false '
        'when cross-project (never "user_profile")' in en
    ), "EN 版应要求凭据关联账号/服务、归 long_term_memory 且标 is_credential=true，不写进 USER.md"
    assert "answering the assistant's question" in en, "EN 版应覆盖回答提问的情形"
    assert "first time or inside a one-off or test task" in en, (
        "EN 版应覆盖首次出现及测试/单次任务"
    )
    assert "usual recipients" not in en, "EN 版不应以 usual recipients 限制首次收件邮箱抽取"
    assert "sender and recipient email" in en, "EN 版应明确覆盖发件与收件邮箱"
    assert "/tmp/test.txt" in en, "EN 版应含单次测试文件 + 邮箱示例"
    assert "sender@example.com" in en and "recipient@example.org" in en, (
        "EN 版示例应使用 RFC 2606 保留示例域名"
    )
    assert "for sending a test file" in en and "without generalizing a one-off test" in en, (
        "EN 版应要求保留单次测试用途，不泛化为默认收件人且不把请求写成已发送"
    )
    assert "follow the Project facts rule below instead" in en, (
        "EN 版应明确项目专用账号/地址让位给项目事实规则"
    )
    assert "SMS verification codes" in en, "EN 版应排除一次性取值"
    assert "never let them suppress" in en, "EN 版应防止临时文件名抑制同句邮箱提取"
    assert "跨项目适用" in cn
    assert "apply across projects" in en
    for prompt in (cn, en):
        assert '"project_fact": true | false' in prompt
        assert '"is_credential": true | false' in prompt
        assert "/work/paper/final" in prompt


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
    assert profile.system_metadata["project"] == "", (
        "全局 user_profile 应在去重前清空来源 project，避免被同项目记录判重跳过"
    )
    if not project.strip():
        assert len(result) == 1, "无项目时应跳过项目候选"
        return
    assert len(result) == 2
    assert result[0].content == project_item["content"]
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert result[0].system_metadata["project"] == project
    assert result[0].system_metadata["project_fact"] == "true"


def test_cross_project_long_term_memory_clears_source_project():
    """跨项目凭据无论误标何类均收口为跨项目 long_term_memory；普通非凭据不清空来源 project。"""
    cred_profile = json.loads(_candidate_json(memory_class="user_profile", tier="semantic"))[0]
    cred_profile.update(
        content="用户的邮箱 SMTP 授权码是 abcd1234。",
        project_fact=False,
        is_credential=True,
    )
    cred_daily = json.loads(_candidate_json(memory_class="daily_memory", tier="semantic"))[0]
    cred_daily.update(
        content="用户的 API 密钥是 key-daily-9999。",
        project_fact=False,
        is_credential=True,
    )
    cred_invalid = json.loads(_candidate_json(memory_class="credential", tier="semantic"))[0]
    cred_invalid.update(
        content="用户的数据库密码是 pg-pass-8888。",
        project_fact=False,
        is_credential=True,
    )
    normal_ltm = json.loads(_candidate_json(memory_class="long_term_memory", tier="semantic"))[0]
    normal_ltm.update(
        content="发布前统一先跑数据库迁移。",
        project_fact=False,
        is_credential=False,
    )
    normal_profile = json.loads(_candidate_json(memory_class="user_profile", tier="semantic"))[0]
    normal_profile.update(
        content="用户的邮箱是 user@example.org。",
        project_fact=False,
        is_credential=False,
    )
    ext, _ = _make(
        responses=[json.dumps([cred_profile, cred_daily, cred_invalid, normal_ltm, normal_profile])]
    )
    source = create_test_unit("u1", "多条混合输入")
    source.system_metadata.update(source_app="jiuwenswarm", project="repo-a")

    result = ext.extract([source])

    assert len(result) == 5
    for idx in (0, 1, 2):
        assert result[idx].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory", (
            "is_credential=True 无论模型误标 user_profile/daily_memory/非法类均应收口为 "
            "long_term_memory"
        )
        assert result[idx].system_metadata["is_credential"] == "true"
        assert result[idx].system_metadata["project"] == "", (
            "跨项目凭据应清空来源 project，保证跨项目可检索"
        )
    assert result[3].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert result[3].system_metadata.get("is_credential") != "true"
    assert result[3].system_metadata["project"] == "repo-a", (
        "普通非凭据 long_term_memory 应保留来源 project，避免 Coding 写入后原项目读不回来"
    )
    assert result[4].system_metadata[MEMORY_CLASS_KEY] == "user_profile"
    assert result[4].system_metadata.get("is_credential") != "true"


@pytest.mark.parametrize("raw_flag", [True, "true", "TRUE", " true "])
def test_string_and_bool_is_credential_normalize_to_long_term_memory(raw_flag):
    """模型返回布尔 true 或字符串 "true" 变体时，均应归一为 is_credential="true" 且归长期记忆。"""
    cred = json.loads(_candidate_json(memory_class="user_profile", tier="semantic"))[0]
    cred.update(
        content="用户的 SMTP 授权码是 auth-str-true-01。",
        project_fact=False,
        is_credential=raw_flag,
    )
    non_cred = json.loads(_candidate_json(memory_class="user_profile", tier="semantic"))[0]
    non_cred.update(
        content="用户的发件邮箱是 sender@example.com。",
        project_fact=False,
        is_credential="false",
    )
    numeric_flag = json.loads(_candidate_json(memory_class="user_profile", tier="semantic"))[0]
    numeric_flag.update(
        content="用户的收件邮箱是 recipient@example.org。",
        project_fact=False,
        is_credential="1",
    )
    ext, _ = _make(responses=[json.dumps([cred, non_cred, numeric_flag])])
    source = create_test_unit("u1", "我的 SMTP 授权码是 auth-str-true-01")
    source.system_metadata.update(source_app="jiuwenswarm", project="repo-a")

    result = ext.extract([source])

    assert len(result) == 3
    assert result[0].system_metadata[MEMORY_CLASS_KEY] == "long_term_memory"
    assert result[0].system_metadata["is_credential"] == "true"
    assert result[0].system_metadata["project"] == ""
    assert result[1].system_metadata[MEMORY_CLASS_KEY] == "user_profile"
    assert result[1].system_metadata.get("is_credential") != "true"
    assert result[2].system_metadata[MEMORY_CLASS_KEY] == "user_profile"
    assert result[2].system_metadata.get("is_credential") != "true"


def test_low_confidence_credential_does_not_taint_same_content_candidate():
    """被 min_confidence 过滤掉的同正文跨项目凭据候选，不得污染有效项目候选的归属。"""
    valid_project = json.loads(_candidate_json(memory_class="long_term_memory", tier="semantic"))[0]
    valid_project.update(
        content="项目部署地址是 https://deploy.repo-a.example.com",
        project_fact=True,
        is_credential=False,
        confidence=1.0,
    )
    dropped_cred = json.loads(_candidate_json(memory_class="long_term_memory", tier="semantic"))[0]
    dropped_cred.update(
        content="项目部署地址是 https://deploy.repo-a.example.com",
        project_fact=False,
        is_credential=True,
        confidence=0.1,
    )
    ext, _ = _make(responses=[json.dumps([valid_project, dropped_cred])])
    source = create_test_unit("u1", "这个项目的部署地址是 https://deploy.repo-a.example.com")
    source.system_metadata.update(source_app="jiuwenswarm", project="repo-a")

    result = ext.extract([source])

    assert len(result) == 1
    assert result[0].system_metadata["project"] == "repo-a", (
        "已过滤的低置信度跨项目凭据候选不得清空有效项目候选的 project"
    )
    assert result[0].system_metadata.get("is_credential") != "true"


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


@pytest.mark.parametrize("team_internal,expected", [(True, 0), (None, 0), (False, 1)])
def test_team_turn_requires_explicit_non_internal_fact(team_internal, expected):
    item = json.loads(_candidate_json(memory_class="long_term_memory"))[0]
    item.update(project_fact=True, team_internal=team_internal)
    extractor, llm = _make(responses=[json.dumps([item])], extractor_type=TeamLlmExtractor)
    source = create_test_unit("u1", "confirmed project fact or team draft")
    source.system_metadata.update(source_app="jiuwenswarm", project="repo", memory_scene="team")
    assert "team_internal" in extractor.extraction_system_prompt([source])
    assert len(extractor.extract([source])) == expected
    assert len(llm.calls) == (2 if team_internal is None else 1)
    source.system_metadata.pop("memory_scene")
    assert "team_internal" in extractor.extraction_system_prompt([source])


def test_generic_extractor_does_not_select_policy_from_app_metadata():
    extractor, _ = _make(responses=[_candidate_json()])
    source = create_test_unit("u1", "durable fact")
    source.system_metadata.update(source_app="jiuwenswarm", memory_scene="team")
    assert "team_internal" not in extractor.extraction_system_prompt([source])
    assert len(extractor.extract([source])) == 1


@pytest.mark.parametrize("fields", [{}, {"team_internal": None}, {"team_internal": "false"},
                                    {"team_internal": 0}])
def test_team_repairs_missing_or_invalid_flag_once(fields):
    item = json.loads(_candidate_json(memory_class="user_profile"))[0]
    item.update(project_fact=False, **fields)
    correction = json.dumps([{"candidate_index": 0, "team_internal": False}])
    extractor, llm = _make([json.dumps([item]), correction], extractor_type=TeamLlmExtractor)
    source = create_test_unit("u1", "confirmed user preference")
    result = extractor.extract([source])
    assert len(result) == 1 and result[0].content == item["content"]
    assert len(llm.calls) == 2
    request = json.loads(llm.calls[1][1].content)
    assert request["candidates"] == [{"candidate_index": 0, "candidate": item}]
    assert request["sources"] == [{"source_id": "u1", "content": source.content}]


@pytest.mark.parametrize("correction,expected", [
    ([{"candidate_index": 2, "team_internal": False}], 2),
    ([{"candidate_index": 2, "team_internal": True}], 1),
    ([{"candidate_index": 2, "team_internal": "false"}], 1),
    ([{"candidate_index": 2, "team_internal": False}] * 2, 1),
    ([{"candidate_index": 99, "team_internal": False}], 1),
    ([], 1),
])
def test_team_correction_preserves_valid_facts_and_original_fields(correction, expected):
    base = json.loads(_candidate_json(memory_class="long_term_memory"))[0]
    items = [dict(base, content="confirmed fact", project_fact=True, team_internal=False),
             dict(base, content="internal draft", project_fact=True, team_internal=True),
             dict(base, content="another fact", project_fact=True)]
    # Attempts to change already-valid decisions or rewrite a repaired fact are ignored.
    response = [{**item, "content": "rewritten", "source_id": "foreign"} for item in correction]
    response.extend([{"candidate_index": 0, "team_internal": True},
                     {"candidate_index": 1, "team_internal": False}])
    extractor, llm = _make(
        [json.dumps(items), json.dumps(response)], extractor_type=TeamLlmExtractor,
    )
    source = create_test_unit("u1", "confirmed facts and internal draft")
    source.system_metadata["project"] = "repo"
    result = extractor.extract([source])
    assert [unit.content for unit in result] == ["confirmed fact", "another fact"][:expected]
    assert len(llm.calls) == 2
    request = json.loads(llm.calls[1][1].content)
    assert [item["candidate_index"] for item in request["candidates"]] == [2]


@pytest.mark.parametrize("failure", [
    RuntimeError("private failure"), TimeoutError("private timeout"), "not JSON",
])
def test_team_correction_failure_keeps_valid_facts_without_another_attempt(failure):
    base = json.loads(_candidate_json(memory_class="user_profile"))[0]
    items = [dict(base, content="confirmed preference", project_fact=False, team_internal=False),
             dict(base, content="private candidate", project_fact=False)]
    extractor, llm = _make(extractor_type=TeamLlmExtractor)
    llm.chat = Mock(side_effect=[json.dumps(items), failure, "[]"])
    with patch("jiuwen_memory.construction.extractor_impl.team_llm_extractor.logger") as log:
        result = extractor.extract([create_test_unit("u1", "user preferences")])
    assert [unit.content for unit in result] == ["confirmed preference"]
    assert llm.chat.call_count == 2
    warning = log.warning.call_args.args
    assert "correction failed" in warning[0] and warning[1] == 1
    assert "private" not in str(log.warning.call_args_list)


@pytest.mark.parametrize("language,source_hint,repair_hint", [
    ("cn", "团队协作回合", "仅纠正缺失或类型错误"),
    ("en", "team collaboration turns", "Repair only missing or invalid"),
    ("unknown", "团队协作回合", "仅纠正缺失或类型错误"),
])
def test_team_initial_and_correction_prompts_follow_language(language, source_hint, repair_hint):
    item = json.loads(_candidate_json(memory_class="user_profile"))[0]
    item["project_fact"] = False
    correction = json.dumps([{"candidate_index": 0, "team_internal": False}])
    extractor, llm = _make(
        [json.dumps([item]), correction], language=language, extractor_type=TeamLlmExtractor,
    )
    assert len(extractor.extract([create_test_unit("u1", "confirmed user preference")])) == 1
    assert len(llm.calls) == 2
    assert source_hint in llm.calls[0][0].content
    assert repair_hint in llm.calls[1][0].content
    assert all("team_internal=false" in call[0].content for call in llm.calls)
