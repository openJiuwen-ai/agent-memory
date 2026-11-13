"""examples/config_experience.yml 的装配守卫：经验样例必须始终可装配。

样例缺失曾作为检视遗留记录（examples/config_experience.yml 长期未产出）；
补齐后以本测试守住它：配置形态随特性演进变化时第一时间红。装配不需要
真实模型——样例里的 openai 配置只在请求期连接，装配期是配置校验与
组件构建（含经验 profile 的 evolver 双算子接线与 dedup_decisions 解析）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.common.type_def import MemoryUnit, matches_memory_unit
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.retrieval.types import RetrievalQuery
from tests.support.whitebox import private

pytestmark = pytest.mark.unit


def test_experience_example_config_assembles() -> None:
    """经验全套样例（双 profile / curator / abstractor skill / Selector）可装配。"""
    path = Path(__file__).resolve().parents[3] / "examples" / "config_experience.yml"
    api = assemble(config=Config.from_yaml(str(path)))
    try:
        assert api is not None, "经验样例配置应完成装配"
        engine = private(api, "_engine")
        assert set(engine.retrieval_pipeline_names()) == {"default", "experience"}, (
            "样例应公开普通与经验两条完整检索流程"
        )
        experience_binding = engine.pipeline.select_for_write([
            MemoryUnit(id="trajectory", system_metadata={"memory_type": "experience"}),
        ])
        experience_evolver = experience_binding.evolver
        evolver_index = private(experience_evolver, "_index")
        engine_index = private(engine, "_index")
        assert evolver_index is experience_binding.index_builder is engine_index, (
            "经验派生记忆应复用 profile 和引擎引用的具名 IndexBuilder"
        )
        vector_builder = private(evolver_index, "_vector_builder")
        index_embedder = private(vector_builder, "_embedder")
        dedup_embedder = private(private(experience_evolver, "_dedup"), "_embedder")
        assert (
            index_embedder is dedup_embedder
        ), "经验索引与去重应共享具名 Embedder，保证向量空间一致"
        mode_evolvers = private(engine, "_mode_evolvers")
        assert (
            mode_evolvers[EvolveMode.EXPERIENCE_CONSOLIDATE].evolver
            is experience_evolver
        ), "经验 dreaming 应继续复用经验写入的完整 Evolver 实例"
        units = [
            MemoryUnit(id=kind, system_metadata={"memory_type": kind})
            for kind in ("semantic", "experience", "skill")
        ]
        for name, expected in (("default", ["semantic"]), ("experience", ["experience"])):
            query = engine.prepare_retrieval_query(RetrievalQuery(text="检索记忆", pipeline=name))
            actual = [unit.id for unit in units if matches_memory_unit(unit, query.filters)]
            assert actual == expected, f"只选择流程 {name} 就应绑定正确的记忆类型范围"
    finally:
        # 样例配 async_timer（周期任务要求）：close 组合释放调度器的后台循环，
        # 不清理会干扰同进程后续测试（http server 用例曾因此偶发失败）。
        api.close()
