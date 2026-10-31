"""经验记忆全链路 e2e——真实 LLM 跑通「轨迹写入 → 评价 → 提炼落盘 → 检索复用」。

与 :mod:`test_middle_e2e_real_llm` 同款凭证模式：项目根 ``.env`` 配置
``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``（``.gitignore``
已忽略）；模块默认 skip，设 ``EXPERIENCE_E2E_RUN=1`` 时启用——全量测试
无凭证环境不受影响。

覆盖 mem2.0 经验记忆的三条真实链路（一条测试函数内顺序推进，共享一个
kernel——轨迹 3 的归因要引用轨迹 1 产出的条目 id）：

1. **轨迹 1（外部验证 success）**：``external_score`` 走确定性判定
   （external 证据不调模型），值得提炼后 ExperienceExtractor 三段模型
   调用（提炼 → 修正 → 分类）产出 fact/tip 条目落经验空间。
2. **轨迹 2（带报错 failure）**：未恢复的 ``is_error`` 工具消息走
   tool_error 证据判失败（不调模型），失败教训被提炼为正向做法。
3. **轨迹 3（model_only 判定 + 归因）**：无外部结果、无报错的轨迹由
   LLM 判定成败（model_only 证据）；``retrieved`` 引用轨迹 1 的条目，
   归因判定写入效用事件。
4. **检索**：``memory_type=experience`` 路由经验 profile，
   ExperienceSelector 选取（复现要求 + 排序分 + 探索名额）后返回条目。

断言只锚定确定性骨架（条目落点 / memory_type / 检索非空），LLM 产出的
具体内容打印到日志供人工检视——模型输出不定型，锚内容即 flaky。
"""

from __future__ import annotations

import json
import os

import pytest
from dotenv import load_dotenv

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import Context, MemoryTier, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control import SpaceSpec
from tests.support.scoped_authenticator import ScopedAuthenticator

load_dotenv()
logger = get_logger(__name__)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        os.environ.get("EXPERIENCE_E2E_RUN", "").strip() != "1",
        reason=(
            "经验记忆真实 LLM e2e 默认跳过——依赖外部凭证与真实模型调用；"
            "需要跑时设 EXPERIENCE_E2E_RUN=1，并确认 .env 已配置 "
            "OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL"
        ),
    ),
]

# -- 身份与空间 ----------------------------------------------------------- #

ORG = "acme"
EXP_SPACE = "shared-experience"
OPS = Scope(org=ORG, user="ops")
ALICE = Scope(org=ORG, user="alice")
EXP_SCOPE = Scope(org=ORG, space=EXP_SPACE)

# 轨迹载体落点（归属主体 alice；经验条目本身由 extractor 落 EXP_SPACE）
WRITE_SCOPE = Scope(org=ORG, user="alice")

_ENGINE_COMPONENT_NAMES = (
    "ingestor",
    "index_builder",
    "retriever",
    "kv_store",
    "scheduler",
    "evolver",
    "lifecycle",
)


def _llm_config() -> dict:
    """从环境变量读取真实 LLM 配置；缺项 skip（同 test_middle_e2e_real_llm）。"""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    missing: list[str] = []
    for name, val in (
        ("OPENAI_API_KEY", api_key),
        ("OPENAI_BASE_URL", base_url),
        ("OPENAI_MODEL", model),
    ):
        if not val:
            missing.append(name)
    if missing:
        pytest.skip(f"未配置 {'/'.join(missing)}，跳过经验记忆真实 LLM e2e")
    return {
        "llm": {
            "default": {
                "target": "openai",
                "params": {
                    "llm_api_key": api_key,
                    "llm_model": model,
                    "llm_base_url": base_url,
                    "llm_temperature": 0.0,
                    "llm_max_tokens": 2048,
                },
            }
        }
    }


def _kernel_config() -> Config:
    """经验记忆全链路装配：cloud 引擎双 profile + 经验算子 + Selector。

    - ``globals.experience``：经验落点固定 EXP_SPACE，开启该空间技能沉淀
      （curated_spaces）——本 e2e 不触发周期整理，但配置形态与生产一致。
    - ``evolver.experience``：DynamicEvolver + experience extractor/appraiser
      （评价包住抽取，F09 决策二）。
    - ``retriever.experience``：PipelineRetriever + ``selector_enabled``——
      Selector 只挂在经验 profile 的 retriever 上，对话检索
      （``retriever.default``）不受选取过滤影响。
    - 空间治理四段照 test_experience_search_routing 的形态。
    """
    config_dict = _llm_config()
    config_dict["globals"] = {
        "experience": {
            "memory_class": "experience",
            "default_space": EXP_SPACE,
            "curated_spaces": [{"space": EXP_SPACE, "skill": True}],
        },
    }
    config_dict["engine"] = {
        "default": {
            "target": "cloud",
            "params": {name: "default" for name in _ENGINE_COMPONENT_NAMES},
        }
    }
    config_dict["evolver"] = {
        # 对话 profile：默认 dynamic（dynamic_llm 抽取器，无评价算子）
        "default": {"target": "dynamic", "params": {}},
        # 经验 profile：评价算子包住 experience 抽取器（F09 决策二）。
        # ``llm`` 键必须显式引用——``Factory.dep`` 只看本实例 params，缺省会
        # 匿名新建 echo（不取 ``llm`` 命名空间的 default），与 defaults.py 同款模式。
        "experience": {
            "target": "dynamic",
            "params": {
                "extractor": "experience",
                "appraiser": "experience",
                "llm": "default",
            },
        },
    }
    config_dict["extractor"] = {
        "experience": {"target": "experience", "params": {"llm": "default"}},
    }
    config_dict["appraiser"] = {
        "experience": {
            "target": "experience",
            "params": {
                # 测试部署密钥：作者摘要的 HMAC salt，e2e 内自洽即可（生产经 ${VAR} 注入）
                "author_key": "experience-e2e-author-key",
                "llm": "default",
                # alice 的外部结果阈值：不配时缺省 0.999（0.9 会判 partial）
                "external_thresholds": {"user:alice": 0.8},
            },
        },
    }
    config_dict["retriever"] = {
        # 对话检索：无 Selector
        "default": {"target": "pipeline", "params": {}},
        # 经验检索：Selector 选取（F09-retrieval 决策三）
        "experience": {"target": "pipeline", "params": {"selector_enabled": True}},
    }
    config_dict["pipeline"] = {
        "default": {
            "target": "metadata",
            "params": {
                "profiles": {
                    "default": {
                        "index_builder": "default",
                        "retriever": "default",
                        "evolver": "default",
                    },
                    "experience": {
                        "index_builder": "default",
                        "retriever": "experience",
                        "evolver": "experience",
                    },
                },
                "fallback": "default",
            },
        }
    }
    config_dict["permission"] = {
        "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
    }
    config_dict["authorizer"] = {
        "default": {
            "target": "space_aware",
            "params": {"delegate": "standard"},
        },
        "standard": {
            "target": "standard",
            "params": {"grant_store": "default", "delegation_store": "default"},
        },
    }
    config_dict["grant_store"] = {"default": "memory"}
    config_dict["delegation_store"] = {"default": "memory"}
    return Config.from_dict(config_dict)


# -- 轨迹 payload --------------------------------------------------------- #


def _deploy_trajectory() -> dict:
    """轨迹 1：成功的 FastAPI 部署任务（外部验证 0.9 + 6 次工具调用）。

    external 证据下判定成败不调模型（external > tool_error > model_only），
    真实 LLM 调用集中在 extractor 的提炼/修正/分类三段。
    """
    return {
        "messages": [
            {
                "role": "user",
                "content": "把 FastAPI 订单服务部署到测试环境，用 uvicorn 启动并验证健康检查",
            },
            {
                "role": "assistant",
                "content": "先确认服务器 Python 版本",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "run_command",
                        "arguments": {"command": "python3 --version"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "Python 3.11.8"},
            {
                "role": "assistant",
                "content": "版本满足，创建虚拟环境并安装依赖",
                "tool_calls": [
                    {
                        "id": "c2",
                        "name": "run_command",
                        "arguments": {
                            "command": (
                                "python3 -m venv .venv && "
                                ".venv/bin/pip install -r requirements.txt"
                            )
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c2",
                "content": "Successfully installed fastapi-0.115.0 uvicorn-0.32.0 pydantic-2.9.0",
            },
            {
                "role": "assistant",
                "content": "依赖就绪，读取测试环境配置",
                "tool_calls": [
                    {
                        "id": "c3",
                        "name": "read_file",
                        "arguments": {"path": "deploy/config_test.yaml"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c3",
                "content": (
                    "db_url: postgresql://orders-test.db:5432/orders\nport: 8300\nworkers: 2"
                ),
            },
            {
                "role": "assistant",
                "content": "配置就绪，用 uvicorn 启动服务",
                "tool_calls": [
                    {
                        "id": "c4",
                        "name": "run_command",
                        "arguments": {
                            "command": (
                                ".venv/bin/uvicorn app.main:app "
                                "--host 0.0.0.0 --port 8300 --workers 2"
                            )
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c4",
                "content": "INFO: Uvicorn running on http://0.0.0.0:8300 (2 workers)",
            },
            {
                "role": "assistant",
                "content": "服务已启动，验证健康检查",
                "tool_calls": [
                    {
                        "id": "c5",
                        "name": "run_command",
                        "arguments": {"command": "curl -sf http://localhost:8300/healthz"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c5",
                "content": '{"status": "ok", "db": "up"}',
            },
            {
                "role": "assistant",
                "content": (
                    "部署完成：FastAPI 订单服务已在测试环境 8300 端口以 2 个 worker 运行，"
                    "健康检查通过，数据库连接正常。"
                ),
            },
        ],
        "capabilities": ["python", "linux", "deployment"],
        "context": {
            "turn": 6,
            "started_at": "2026-10-24T09:00:00+08:00",
            "ended_at": "2026-10-24T09:04:30+08:00",
            "end_state": "completed",
            "project": "orders-service",
            "model": "e2e-agent",
            "framework": "agent-core",
        },
    }


def _failed_worker_trajectory() -> dict:
    """轨迹 2：启动队列 worker 失败（未恢复的 is_error，教训提炼为正向做法）。

    未恢复报错走 tool_error 证据判失败（不调模型）；``has_error_mark``
    命中「值得提炼」四条之一——已恢复的报错同样值得提炼，这里故意留一个
    未恢复的，让失败教训的提炼路径真实跑一遍。
    """
    return {
        "messages": [
            {
                "role": "user",
                "content": "在测试服务器上启动 Redis 队列 worker，处理订单异步任务",
            },
            {
                "role": "assistant",
                "content": "先看当前有没有已运行的 worker 进程",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "run_command",
                        "arguments": {"command": "ps aux | grep worker"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "(no worker processes)"},
            {
                "role": "assistant",
                "content": "没有现成进程，直接启动 worker",
                "tool_calls": [
                    {
                        "id": "c2",
                        "name": "run_command",
                        "arguments": {
                            "command": "python3 -m worker.main --queue orders"
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c2",
                "content": (
                    "ERROR: connection to Redis at redis-test.db:6379 failed - Connection refused"
                ),
                "is_error": True,
            },
            {
                "role": "assistant",
                "content": "配置里的 Redis 地址连不上，换默认地址 localhost 再试",
                "tool_calls": [
                    {
                        "id": "c3",
                        "name": "run_command",
                        "arguments": {
                            "command": (
                                "python3 -m worker.main --queue orders --redis localhost:6379"
                            )
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c3",
                "content": (
                    "ERROR: connection to Redis at localhost:6379 failed - Connection refused"
                ),
                "is_error": True,
            },
            {
                "role": "assistant",
                "content": (
                    "两个地址都连接拒绝，测试环境的 Redis 实例大概率没有启动。"
                    "我无法完成启动 worker 的任务，需要运维先恢复 Redis 服务。"
                ),
            },
        ],
        "capabilities": ["python", "redis"],
        "context": {
            "turn": 3,
            "started_at": "2026-10-24T10:00:00+08:00",
            "ended_at": "2026-10-24T10:02:10+08:00",
            "end_state": "aborted",
            "project": "orders-service",
            "model": "e2e-agent",
            "framework": "agent-core",
        },
    }


def _second_deploy_trajectory(retrieved_id: str) -> dict:
    """轨迹 3：第二个部署任务——model_only 证据由 LLM 判成败 + 归因取用记录。

    无 external_score、无报错：判定成败走 model_only（LLM 读轨迹判
    success/partial/failure）。``retrieved`` 引用轨迹 1 产出的条目——
    归因步由 LLM 判该条目对本次结果 helpful/harmful，写效用事件。
    """
    return {
        "messages": [
            {
                "role": "user",
                "content": "再部署一个 FastAPI 服务到测试环境，这次是库存服务 inventory-service",
            },
            {
                "role": "assistant",
                "content": "沿用上次部署订单服务的流程：先装依赖",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "run_command",
                        "arguments": {
                            "command": (
                                "python3 -m venv .venv && "
                                ".venv/bin/pip install -r requirements.txt"
                            )
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "Successfully installed fastapi-0.115.0 uvicorn-0.32.0",
            },
            {
                "role": "assistant",
                "content": "读取库存服务的测试配置",
                "tool_calls": [
                    {
                        "id": "c2",
                        "name": "read_file",
                        "arguments": {"path": "deploy/config_test.yaml"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c2",
                "content": (
                    "db_url: postgresql://inventory-test.db:5432/inventory"
                    "\nport: 8310\nworkers: 2"
                ),
            },
            {
                "role": "assistant",
                "content": "用 uvicorn 在 8310 端口启动",
                "tool_calls": [
                    {
                        "id": "c3",
                        "name": "run_command",
                        "arguments": {
                            "command": (
                                ".venv/bin/uvicorn app.main:app "
                                "--host 0.0.0.0 --port 8310 --workers 2"
                            )
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c3",
                "content": "INFO: Uvicorn running on http://0.0.0.0:8310 (2 workers)",
            },
            {
                "role": "assistant",
                "content": "验证健康检查",
                "tool_calls": [
                    {
                        "id": "c4",
                        "name": "run_command",
                        "arguments": {"command": "curl -sf http://localhost:8310/healthz"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c4",
                "content": '{"status": "ok", "db": "up"}',
            },
            {
                "role": "assistant",
                "content": "库存服务部署完成：8310 端口运行正常，健康检查与数据库连接都通过。",
            },
        ],
        "capabilities": ["python", "linux", "deployment"],
        "retrieved": [{"id": retrieved_id, "ref": "0.n"}],
        "context": {
            "turn": 5,
            "started_at": "2026-10-24T11:00:00+08:00",
            "ended_at": "2026-10-24T11:03:00+08:00",
            "end_state": "completed",
            "project": "inventory-service",
            "model": "e2e-agent",
            "framework": "agent-core",
        },
    }


# -- 写入与检视辅助 ------------------------------------------------------- #


def _add_trajectory(api, payload: dict, trajectory_id: str, **summary: object) -> list:
    """经验轨迹写入：content 是轨迹 JSON，metadata 带 infer 与概要字段。

    ``memory_type=experience`` + ``infer=true`` 走经验 profile 的
    evolver EXTRACT（评价 → 提炼 → 去重 → 落盘 → finalize）。
    """
    units = api.add(
        json.dumps(payload, ensure_ascii=False),
        WRITE_SCOPE,
        security=internal_context(ScopedAuthenticator(ALICE)),
        system_metadata={
            "memory_type": "experience",
            "trajectory_id": trajectory_id,
            "infer": "true",
            **{k: str(v) for k, v in summary.items()},
        },
    )
    logger.info("[write] %s -> %d derived unit(s)", trajectory_id, len(units))
    for unit in units:
        logger.info(
            "  - id=%s tier=%s space=%r tags=%s",
            unit.id[:8],
            unit.tier.value,
            unit.scope.space,
            unit.tags,
        )
        logger.info("    content=%r", unit.content[:200])
    return units


def _log_experience_entries(api) -> int:
    """列出经验空间全部条目并打印；返回条数（断言与检视共用）。

    ``memory_types`` 必须点名 ``experience``：list 的类型隔离谓词（mem2.0 §2.3）
    在未点名时默认不混入经验条目——不带参数查询会拿到 0 条（设计行为）。
    """
    result = api.list(
        EXP_SCOPE,
        security=internal_context(ScopedAuthenticator(ALICE)),
        memory_types=["experience"],
    )
    logger.info("[list] %s entries in %s", len(result.items), EXP_SPACE)
    for unit in result.items:
        logger.info(
            "  - id=%s tier=%s lifecycle=%s memory_type=%s",
            unit.id[:8],
            unit.tier.value,
            unit.lifecycle.value,
            unit.system_metadata.get("memory_type", ""),
        )
        logger.info("    content=%r", unit.content[:200])
    return len(result.items)


# -- e2e 主链路 ----------------------------------------------------------- #


def test_experience_full_pipeline_with_real_llm() -> None:
    """轨迹写入 → 评价 → 提炼落盘 → 检索复用，真实 LLM 全链路一次跑通。"""
    kernel = build_kernel(config=_kernel_config())
    api = kernel.api
    # 经验空间由 ops（ADMIN）创建，alice 为 owner——写入与检索都判权通过
    api.create_space(
        SpaceSpec(org=ORG, space=EXP_SPACE, owner=ALICE),
        security=internal_context(ScopedAuthenticator(OPS, role=Role.ADMIN)),
    )

    # ---- 轨迹 1：成功部署（external 证据 + extractor 三段模型调用） ----
    units_1 = _add_trajectory(
        api,
        _deploy_trajectory(),
        "traj-e2e-deploy-001",
        tool_call_count=6,
        external_score=0.9,
    )
    assert units_1, "轨迹 1（external success）应产出至少一条经验条目"
    for unit in units_1:
        assert unit.scope == EXP_SCOPE, f"经验条目应落经验空间，got {unit.scope}"
        assert (
            unit.system_metadata.get("memory_type") == "experience"
        ), f"条目 memory_type 应为 experience，got {unit.system_metadata.get('memory_type')}"
        # tier 与条目类别对应：fact 落 SEMANTIC、可执行条件动作（tip）落 PROCEDURAL
        # （extractor 落盘约定，experience_extractor.py MemoryTier.PROCEDURAL if is_tip）。
        assert unit.tier in (MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL), (
            f"经验条目 tier 应为 SEMANTIC/PROCEDURAL，got {unit.tier}"
        )
    count_after_1 = _log_experience_entries(api)
    assert count_after_1 >= len(units_1), "经验空间条数应覆盖轨迹 1 产出"

    # ---- 轨迹 2：失败教训（tool_error 证据，规则判定不调模型） ----
    # 判定为 failure（未恢复报错，judged_by=rule）；提炼 prompt 只提炼观察到成功
    # 的做法，全失败轨迹无可提炼——真实模型对此可能返回空文本（_parse_object
    # 的空白响应防御），产出数不作硬断言，验证的是链路走通不炸。
    units_2 = _add_trajectory(
        api,
        _failed_worker_trajectory(),
        "traj-e2e-worker-001",
        tool_call_count=3,
    )
    logger.info("[traj2] failure 轨迹产出 %d 条（全失败无成功做法时为 0）", len(units_2))
    count_after_2 = _log_experience_entries(api)
    assert count_after_2 >= count_after_1, "轨迹 2 写入后经验空间条数不应减少"

    # ---- 轨迹 3：model_only 判定 + 归因（LLM 判成败与取用条目效用） ----
    # 输出条数不作断言：与轨迹 1 提炼的条目语义相近时可能被去重判定 NOOP/UPDATE
    _add_trajectory(
        api,
        _second_deploy_trajectory(units_1[0].id),
        "traj-e2e-deploy-002",
        tool_call_count=5,
        retrieved_count=1,
    )
    _log_experience_entries(api)

    # ---- 检索：经验 profile + Selector 选取 ----
    result = api.search(
        "部署 FastAPI 服务到测试环境",
        Context(scope=EXP_SCOPE, extensions={"memory_type": "experience"}),
        security=internal_context(ScopedAuthenticator(ALICE)),
        top_k=5,
    )
    logger.info("[search] hits=%d errors=%d", len(result.items), len(result.errors))
    for item in result.items:
        logger.info(
            "  - id=%s score=%.3f level=%s content=%r",
            item.unit_id[:8],
            item.score,
            item.level.value,
            (item.abstract or item.content or "")[:160],
        )
    assert result.items, (
        f"经验检索应命中条目（探索名额保底），got errors={result.errors}"
    )
    # 真源对照同样要点名 memory_types（list 的类型隔离谓词，见 _log_experience_entries）。
    listed = api.list(
        EXP_SCOPE,
        security=internal_context(ScopedAuthenticator(ALICE)),
        memory_types=["experience"],
    ).items
    true_source_ids = {u.id for u in listed}
    for item in result.items:
        assert item.unit_id in true_source_ids, (
            f"检索命中的 {item.unit_id} 应是经验空间的真源条目"
        )
