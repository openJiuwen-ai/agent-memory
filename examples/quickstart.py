"""最小功能集端到端演示：add → search → get（+ update / 治理 / admin）。

运行：``PYTHONPATH=. python3 examples/quickstart.py``（或先 ``pip install -e .``）

**配置从 YAML 读、合并到内置默认后装配**：``Config.from_yaml(examples/config.yml)`` 解析成
两级命名空间（``AssemblyContext``）→ ``assemble`` 把它合并覆盖到内置默认上，各组件级 Producer
（KvProducer / RecallerProducer / EngineProducer …，工厂均与契约同处接口层、注册式）经
``build_named`` / ``dep`` 按引用产出实例、按具名共享单例，``build_kernel`` 编排成 ``MemoryAPI``。
改实现/换后端只改 yml，各层不动。无任何外部依赖。
"""

from __future__ import annotations

import logging
import os

from jiuwen_memory.api import assemble
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control.types import MemoryPatch

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
    actor = scope  # 本人操作自己的 scope

    # 1) add -----------------------------------------------------------------
    facts = [
        "Alice 喜欢在早上喝美式咖啡，不加糖。",
        "项目 agent-memory 的目标是给 AI agent 提供独立记忆子系统。",
        "下周三下午三点和设计团队评审检索链路。",
    ]
    written_ids = []
    for f in facts:
        units = api.add(f, scope, identity=actor, tags=["demo"])
        written_ids.append(units[0].id)
        logger.info("[add] %s  <%s...>", units[0].id[:8], f[:24])

    # 2) search --------------------------------------------------------------
    logger.info("\n[search] query='咖啡 早上'")
    res = api.search("咖啡 早上", Context(scope), identity=actor, top_k=3, with_trajectory=True)
    for item in res.items:
        logger.info("  score=%.3f  %s  %s", item.score, item.unit_id[:8], item.content)
    logger.info("  trajectory: %s", [(s.stage, s.candidate_count) for s in res.trajectory])

    # 3) get（tier 由构建层 Classifier 在写入时判定：含「喜欢」→ semantic） ----
    first = written_ids[0]
    got = api.get(first, scope, identity=actor)
    logger.info(
        "\n[get] %s  tier=%s  tags=%s  content=<%s>",
        first[:8],
        got.tier.value,
        got.tags,
        got.content,
    )

    # 4) update（SUPERSEDE，记版本链） ---------------------------------------
    new_unit = api.update(
        first, scope, MemoryPatch(content="Alice 改喝拿铁了，要加燕麦奶。"), identity=actor
    )
    logger.info(
        "\n[update] %s -> %s  supersedes=%s", first[:8], new_unit.id[:8], new_unit.supersedes[:8]
    )
    chain = api.trace(new_unit.id, scope, identity=actor)
    logger.info("  trace chain: %s", [u.id[:8] for u in chain])

    # 4.5) evolve（构建层闭环：抽取低抽象事实 / 升华画像 / 遗忘被取代的旧版） --
    q = "咖啡 项目 评审"
    before = len(api.search(q, Context(scope), identity=actor, top_k=20).items)
    api.evolve(scope, EvolveMode.EXTRACT, identity=actor)  # Extractor：派生事实(记血缘)
    api.evolve(scope, EvolveMode.CONSOLIDATE, identity=actor)  # Abstractor：升华 CORE 画像
    api.evolve(scope, EvolveMode.ASSOCIATE, identity=actor)  # Associator：发现关联
    api.evolve(scope, EvolveMode.FORGET, identity=actor)  # 清理 superseded 旧版
    after = len(api.search(q, Context(scope), identity=actor, top_k=20).items)
    logger.info(
        "\n[evolve] 召回命中 %s -> %s（extract 派生 + consolidate 画像入索引）", before, after
    )
    prof = api.search("画像综合", Context(scope), identity=actor, top_k=1).items
    if prof:
        logger.info("  consolidate 画像 %s: <%s...>", prof[0].unit_id[:8], prof[0].content[:36])

    # 5) admin + audit -------------------------------------------------------
    logger.info("\n[admin] policies: %s", api.admin_all(identity=actor))
    logger.info("[audit] add 事件数: %s", len(api.audit({"action": "add"}, identity=actor)))


if __name__ == "__main__":
    main()
