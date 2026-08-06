"""最小功能集端到端演示：write → recall → get（+ update / 治理 / admin）。

运行：``PYTHONPATH=src python3 examples/quickstart.py``

**配置从 YAML 读、合并到内置默认后装配**：``Config.from_yaml(examples/config.yml)`` 解析成
两级命名空间（``AssemblyContext``）→ ``assemble`` 把它合并覆盖到内置默认上，各组件级 Producer
（KvProducer / RecallerProducer / EngineProducer …，工厂均与契约同处接口层、注册式）经
``build_named`` / ``dep`` 按引用产出实例、按具名共享单例，``build_kernel`` 编排成 ``MemoryAPI``。
改实现/换后端只改 yml，各层不动。无任何外部依赖。
"""

from __future__ import annotations

import logging
import os

from api import assemble
from common.security import internal_context
from common.security.authentication.authentication_impl.dev_authenticator import (
    DevAuthenticator,
)
from common.type_def import Context, Scope
from config import Config
from construction import EvolveMode
from control.types import MemoryPatch

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml")
    config = Config.from_yaml(cfg_path) if os.path.exists(cfg_path) else Config()
    if not config.is_empty():
        g = config.context().globals
        logger.info(
            "[config] 来自 %s（合并到内置默认）: vector=%s graph=%s rerank=%s",
            os.path.basename(cfg_path),
            g.get("vector_enabled"),
            g.get("graph_enabled"),
            g.get("rerank_enabled"),
        )
    else:
        logger.info("[config] 无用户配置，按内置默认装配（纯内存离线）")
    api = assemble(config=config)
    scope = Scope(org="acme", user="alice", agent="assistant", session="s1")
    # 身份由认证能力产出，不由脚本声明（F05 §进程内调用）：`scope` 只说「操作哪个
    # 范围」，`security` 才说「谁在操作」。进程内直连缺省是 dev 认证（恒 ROOT）。
    security = internal_context(DevAuthenticator())

    # 1) write ---------------------------------------------------------------
    facts = [
        "Alice 喜欢在早上喝美式咖啡，不加糖。",
        "项目 agent-memory 的目标是给 AI agent 提供独立记忆子系统。",
        "下周三下午三点和设计团队评审检索链路。",
    ]
    written_ids = []
    for f in facts:
        units = api.write(f, scope, security=security, tags=["demo"])
        written_ids.append(units[0].id)
        logger.info("[write] %s  <%s...>", units[0].id[:8], f[:24])

    # 2) recall --------------------------------------------------------------
    logger.info("\n[recall] query='咖啡 早上'")
    res = api.recall("咖啡 早上", Context(scope), security=security, top_k=3, with_trajectory=True)
    for item in res.items:
        logger.info("  score=%.3f  %s  %s", item.score, item.unit_id[:8], item.content)
    logger.info("  trajectory: %s", [(s.stage, s.candidate_count) for s in res.trajectory])

    # 3) get（tier 由构建层 Classifier 在写入时判定：含「喜欢」→ semantic） ----
    first = written_ids[0]
    got = api.get(first, scope, security=security)
    logger.info(
        "\n[get] %s  tier=%s  tags=%s  content=<%s>",
        first[:8],
        got.tier.value,
        got.tags,
        got.content,
    )

    # 4) update（SUPERSEDE，记版本链） ---------------------------------------
    new_unit = api.update(
        first, scope, MemoryPatch(content="Alice 改喝拿铁了，要加燕麦奶。"), security=security
    )
    logger.info(
        "\n[update] %s -> %s  supersedes=%s", first[:8], new_unit.id[:8], new_unit.supersedes[:8]
    )
    chain = api.trace(new_unit.id, scope, security=security)
    logger.info("  trace chain: %s", [u.id[:8] for u in chain])

    # 4.5) evolve（构建层闭环：抽取低抽象事实 / 升华画像 / 遗忘被取代的旧版） --
    q = "咖啡 项目 评审"
    before = len(api.recall(q, Context(scope), security=security, top_k=20).items)
    api.evolve(scope, EvolveMode.EXTRACT, security=security)  # Extractor：派生事实(记血缘)
    api.evolve(scope, EvolveMode.CONSOLIDATE, security=security)  # Abstractor：升华 CORE 画像
    api.evolve(scope, EvolveMode.ASSOCIATE, security=security)  # Associator：发现关联
    api.evolve(scope, EvolveMode.FORGET, security=security)  # 清理 superseded 旧版
    after = len(api.recall(q, Context(scope), security=security, top_k=20).items)
    logger.info(
        "\n[evolve] 召回命中 %s -> %s（extract 派生 + consolidate 画像入索引）", before, after
    )
    prof = api.recall("画像综合", Context(scope), security=security, top_k=1).items
    if prof:
        logger.info("  consolidate 画像 %s: <%s...>", prof[0].unit_id[:8], prof[0].content[:36])

    # 5) admin + audit -------------------------------------------------------
    logger.info("\n[admin] policies: %s", api.admin_all(security=security))
    logger.info("[audit] write 事件数: %s", len(api.audit({"action": "write"}, security=security)))


if __name__ == "__main__":
    main()
