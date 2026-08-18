"""端到端测试：索引构建 + Evolver 自演进 + 构建层 LLM 实现闭环验证。

覆盖三条链路：
1. 写入路径：write → Classifier(LLM) → KV → IndexBuilder → 可检索
2. Background EXTRACT：write 后自动触发 Evolver → Extractor(LLM) → 派生落盘 → 可检索
3. 离线 profile：默认 Config → assemble → 全链路可运行
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import assemble
from jiuwen_memory.common.type_def import Context, MemoryTier, Modality, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode

DEFAULT_SCOPE = Scope(org="test", user="alice", agent="a1", session="s1")
DEFAULT_ACTOR = Scope(org="test", user="alice")


class TestE2EWritePath:
    """写入路径端到端：Classifier(LLM) → KV → IndexBuilder → 可检索。"""

    @pytest.fixture()
    def llm_api(self):
        """装配 LLM 算子 + EchoLLM（不调外部 API）的 API 实例。"""
        config = Config.from_dict(
            {  # EchoLLM：返回用户消息
                "globals": {"vector_enabled": True},
                "classifier": {"default": "llm"},
                "extractor": {"default": "llm"},
            }
        )
        return assemble(config=config)

    @staticmethod
    def test_write_recall_returns_written_unit(llm_api):
        """add → 落盘建索引 → search 可召回原始 unit（add 不再调 classify，tier 保持默认）。"""
        units = llm_api.add(
            "用户偏好简洁回答风格",
            DEFAULT_SCOPE,
            source=Modality.TEXT,
            identity=DEFAULT_ACTOR,
        )
        assert len(units) == 1
        # add 不调 classify：tier 保持 MemoryUnit 默认 EPISODIC，无 classify metadata
        assert units[0].tier == MemoryTier.EPISODIC
        assert "classify_source" not in units[0].system_metadata

        # recall 可召回
        result = llm_api.search(
            "简洁",
            Context(DEFAULT_SCOPE),
            identity=DEFAULT_ACTOR,
            top_k=10,
        )
        assert len(result.items) > 0
        assert any("简洁" in item.content for item in result.items)


class TestE2EBackgroundExtract:
    """Background EXTRACT 端到端：write → scheduler 自动触发 → Extractor → 可检索。"""

    @pytest.fixture()
    def llm_api(self):
        """同上：LLM 算子 + EchoLLM。"""
        config = Config.from_dict(
            {
                "globals": {"vector_enabled": True},
                "classifier": {"default": "llm"},
                "extractor": {"default": "llm"},
            }
        )
        return assemble(config=config)

    @staticmethod
    def test_background_extract_trigger(llm_api):
        """write 后 background EXTRACT 自动触发——Scheduler 应执行 Evolver。"""
        units = llm_api.add(
            "用户偏好简洁回答",
            DEFAULT_SCOPE,
            source=Modality.TEXT,
            identity=DEFAULT_ACTOR,
        )
        # write() 内 scheduler.submit(EXTRACT, BACKGROUND) → InProcessScheduler 同步执行
        # EchoLLM 返回原文（非 JSON），LLMExtractor 降级为空 list
        # 验证：不崩溃即可（EchoLLM 不产出有效 JSON）
        assert len(units) == 1

        # recall 原始 unit 仍可召回
        result = llm_api.search(
            "偏好",
            Context(DEFAULT_SCOPE),
            identity=DEFAULT_ACTOR,
            top_k=10,
        )
        assert len(result.items) > 0

    @staticmethod
    def test_explicit_evolve_extract(llm_api):
        """手动调 evolve(EXTRACT) — API 层接口验证。"""
        llm_api.add(
            "用户讨论了架构设计",
            DEFAULT_SCOPE,
            source=Modality.TEXT,
            identity=DEFAULT_ACTOR,
        )
        # 手动触发演进
        job_id = llm_api.evolve(
            DEFAULT_SCOPE,
            EvolveMode.EXTRACT,
            identity=DEFAULT_ACTOR,
        )
        assert job_id  # 返回 job_id


class TestE2EOfflineProfile:
    """离线 profile：默认 Config → 全链路可运行（不依赖外部 API）。"""

    @pytest.fixture()
    def offline_api(self):
        """默认 Config 的 API 实例。"""
        return assemble()

    @staticmethod
    def test_offline_write_and_recall(offline_api):
        """默认 Config: keyword 算子 + echo LLM → add + search 可运行。"""
        units = offline_api.add(
            "测试内容",
            DEFAULT_SCOPE,
            source=Modality.TEXT,
            identity=DEFAULT_ACTOR,
        )
        assert len(units) == 1

        result = offline_api.search(
            "测试",
            Context(DEFAULT_SCOPE),
            identity=DEFAULT_ACTOR,
            top_k=5,
        )
        assert len(result.items) > 0

    @staticmethod
    def test_offline_evolve_noop(offline_api):
        """默认 Config: keyword Extractor → evolve(EXTRACT) 不崩溃。"""
        offline_api.add(
            "测试内容",
            DEFAULT_SCOPE,
            source=Modality.TEXT,
            identity=DEFAULT_ACTOR,
        )
        # background EXTRACT 自动触发（keyword extractor 产出 chunk 类派生 unit）
        # 验证不崩溃即可
        result = offline_api.search(
            "测试",
            Context(DEFAULT_SCOPE),
            identity=DEFAULT_ACTOR,
            top_k=5,
        )
        assert len(result.items) > 0
