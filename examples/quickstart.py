# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小功能集端到端演示：add → search → get（+ update / 治理 / admin）。

运行：``PYTHONPATH=. python3 examples/quickstart.py``（或先 ``pip install -e .``）

**配置从 YAML 读、合并到内置默认后装配**：``Config.from_yaml(examples/config.yml)`` 解析成
两级命名空间（``AssemblyContext``）→ ``assemble`` 把它合并覆盖到内置默认上，各组件级 Producer
（KvProducer / RecallerProducer / EngineProducer …，工厂均与契约同处接口层、注册式）经
``build_named`` / ``dep`` 按引用产出实例、按具名共享单例，``build_kernel`` 编排成 ``MemoryAPI``。
改实现/换后端只改 yml，各层不动。无任何外部依赖。

群体记忆默认关闭；取消 ``config.yml`` 末块的注释即开启，本文件各节随之带上空间维度，
末节再演示按归属坐标分流写入、跨空间检索与空间隔离。两种形态下各节的调用形式相同，
差别只在开启后须先开通空间——写入未注册空间由放行改为拒绝。
"""

from __future__ import annotations

import logging
import os

from jiuwen_memory.api import assemble
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.router import (
    Router,
    RouterProducer,
    RouteTable,
    build_decision,
    parse_route_table,
)
from jiuwen_memory.control import SpaceMember, SpaceSpec
from jiuwen_memory.control.types import MemoryPatch

logger = logging.getLogger(__name__)


ORG = "acme"
ALICE = Scope(org=ORG, user="alice", agent="assistant", session="s1")
ALICE_PERSON = Scope(org=ORG, user="alice")  # 治理动作要求本人直接调用，不带代理维
BOB = Scope(org=ORG, user="bob", agent="assistant", session="s2")
OPS = Scope()  # 运维身份：管理面入口按根 scope 鉴权

# 接口先行过渡桥接：把 identity Scope 包成 RequestSecurityContext（实装后由认证产出）
SEC_ALICE = legacy_request_context(ALICE)
SEC_ALICE_PERSON = legacy_request_context(ALICE_PERSON)
SEC_BOB = legacy_request_context(BOB)
SEC_OPS = legacy_request_context(OPS)
MAIN_SPACE = "u_alice"  # 开启空间治理后 alice 的主空间
PROJECT_SPACE = "p_apollo"


class _KeywordRouter(Router):
    """按关键词判类别的演示桩，使群体记忆一节可离线运行。

    生产用 ``llm``：一批候选发一次模型调用，逐条产出「命中哪个类别」与「哪些收窄维为真」。
    落点解析、记录维标签、fallback 回落与两个落盘不变量都不在实现内部——它们由
    ``construction.router`` 的公共函数承担，换一个实现不会漏掉。
    """

    def __init__(self, table: RouteTable) -> None:
        self._table = table

    @property
    def table(self) -> RouteTable:
        return self._table

    def operator_type(self) -> OperatorType:
        return OperatorType.ROUTER

    def health(self) -> None:
        return None

    def route(self, units, ctx):
        decisions = []
        for unit in units:
            is_project = "项目" in unit.content or "apollo" in unit.content.lower()
            memory_class = "project_memory" if is_project else "user_memory"
            # 个人偏好判为与当前项目相关：落个人主空间，但检索时能按项目收窄出来
            decisions.append(build_decision(unit, memory_class, ("project_id",), ctx))
        return decisions


@RouterProducer.register("keyword_demo")
def _build_keyword_demo(config):
    return _KeywordRouter(
        parse_route_table(
            {
                "coord_entities": config.get("coord_entities"),
                "memory_classes": config.get("memory_classes"),
                "narrow_dims": config.get("narrow_dims"),
            }
        )
    )


def _provision(api) -> Scope:
    """开启空间治理后，先开通主空间并把工作 scope 指向它。

    启用后写入未注册空间由放行改为拒绝，``space`` 留空的写入不再放行——这是开关带来的
    唯一形态差异，前面各节的调用形式不变。
    """
    api.create_space(SpaceSpec(org=ORG, space=MAIN_SPACE, owner=ALICE_PERSON), security=SEC_OPS)
    logger.info("[space] 已开通主空间 %s（个体形态，成员表为空）", MAIN_SPACE)
    # 取 org + space 两维：条目落盘时 scope 就归一成这两维，主体维与会话维被去掉，
    # 带主体维的 scope 写得进去却读不回来。调用方身份另经 identity 传入。
    return Scope(org=ORG, space=MAIN_SPACE)


def _collective(api) -> None:
    """群体记忆：按归属坐标分流写入、跨空间检索、空间隔离。

    与前面各节共用同一个内核——开关开着时前面的写入落在主空间，这里再加一个协作空间，
    演示分流与隔离。判定表为空即整条路径不可达，跳过并提示开启方式。
    """
    rule = "=" * 72
    if api.route_table.is_empty():
        logger.info(
            "\n%s\n[collective] 群体记忆：未开启归属判定，跳过\n%s\n"
            "  取消 config.yml 末块的注释即可看到按归属坐标分流写入、跨空间检索与空间隔离",
            rule,
            rule,
        )
        return
    logger.info("\n%s\n[collective] 群体记忆：分流写入 / 跨空间检索 / 空间隔离\n%s", rule, rule)

    # 1) 协作空间：由项目负责人建，逐参与者写一条成员记录，成员表非空即共享形态成立
    api.create_space(SpaceSpec(org=ORG, space=PROJECT_SPACE, owner=ALICE_PERSON), security=SEC_OPS)
    api.add_space_member(
        ORG,
        PROJECT_SPACE,
        SpaceMember(
            scope=Scope(org=ORG, user="bob"),
            content_role=SpaceContentRole.EDITOR,
            governance_role=SpaceGovernanceRole.NONE,
        ),
        security=SEC_ALICE_PERSON,
    )
    logger.info("  [space] 开通协作空间 %s，写入 bob 的成员记录", PROJECT_SPACE)

    # 2) 写入：参数袋带 coords 键即交由判定，落点由归属坐标与内容判定共同决定
    coords = {"project": "apollo"}
    preference = "我习惯用简洁的风格回复"
    project_fact = "项目 apollo 的部署环境是集群 A"
    for text in (preference, project_fact):
        units = api.add(
            text, Scope(org=ORG), security=SEC_ALICE, system_metadata={"coords": coords}
        )
        logger.info("  [add] <%s...> -> %s", text[:12], units[0].scope.space)

    # 3) 跨空间检索：仍是 search，extensions 带 spaces 键即转跨空间形态；空列表表示
    #    「调用方可读的全部空间」。一次调用并发召回候选空间，归属坐标转成第二族收窄谓词
    query = "apollo 部署 风格"
    ctx = Context(scope=Scope(org=ORG), extensions={"coords": coords, "spaces": []})
    mine = {item.content for item in api.search(query, ctx, security=SEC_ALICE, top_k=10).items}
    logger.info(
        "  [search across spaces] alice 命中 %s 条，含个人偏好与项目事实：%s / %s",
        len(mine),
        preference in mine,
        project_fact in mine,
    )

    # 4) 隔离：bob 是协作空间的成员，读得到项目事实，读不到 alice 主空间的偏好
    theirs = {item.content for item in api.search(query, ctx, security=SEC_BOB, top_k=10).items}
    logger.info(
        "  [isolation] bob 命中 %s 条，项目事实可见：%s，alice 的偏好不可见：%s",
        len(theirs),
        project_fact in theirs,
        preference not in theirs,
    )


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
    # 开启空间治理后写入未注册空间由放行改为拒绝，因此先开通主空间、scope 指向它；
    # 未开启时空间维留空，与改造前一致。后面各节的调用形式两种形态下完全相同。
    scope = _provision(api) if api.space_governance_enabled else ALICE
    actor = ALICE  # 调用方身份始终是本人，与目标 scope 分开
    security = legacy_request_context(actor)

    # 1) add -----------------------------------------------------------------
    facts = [
        "Alice 喜欢在早上喝美式咖啡，不加糖。",
        "项目 agent-memory 的目标是给 AI agent 提供独立记忆子系统。",
        "下周三下午三点和设计团队评审检索链路。",
    ]
    written_ids = []
    for f in facts:
        units = api.add(f, scope, security=security, tags=["demo"])
        written_ids.append(units[0].id)
        logger.info("[add] %s  <%s...>", units[0].id[:8], f[:24])

    # 2) search --------------------------------------------------------------
    logger.info("\n[search] query='咖啡 早上'")
    res = api.search("咖啡 早上", Context(scope), security=security, top_k=3, with_trajectory=True)
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
    before = len(api.search(q, Context(scope), security=security, top_k=20).items)
    api.evolve(scope, EvolveMode.EXTRACT, security=security)  # Extractor：派生事实(记血缘)
    api.evolve(scope, EvolveMode.CONSOLIDATE, security=security)  # Abstractor：升华 CORE 画像
    api.evolve(scope, EvolveMode.ASSOCIATE, security=security)  # Associator：发现关联
    api.evolve(scope, EvolveMode.FORGET, security=security)  # 清理 superseded 旧版
    after = len(api.search(q, Context(scope), security=security, top_k=20).items)
    logger.info(
        "\n[evolve] 召回命中 %s -> %s（extract 派生 + consolidate 画像入索引）", before, after
    )
    prof = api.search("画像综合", Context(scope), security=security, top_k=1).items
    if prof:
        logger.info("  consolidate 画像 %s: <%s...>", prof[0].unit_id[:8], prof[0].content[:36])

    # 5) admin + audit（管理面入口按根 scope 鉴权，须用运维身份，普通用户会被拒）--
    logger.info("\n[admin] policies: %s", api.admin_all(security=SEC_OPS))
    logger.info("[audit] add 事件数: %s", len(api.audit({"action": "add"}, security=SEC_OPS)))

    # 6) 群体记忆：在同一内核上加一个协作空间，演示分流写入、跨空间检索与隔离 -----
    _collective(api)


if __name__ == "__main__":
    main()
