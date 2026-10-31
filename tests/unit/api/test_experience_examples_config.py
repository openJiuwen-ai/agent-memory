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
from jiuwen_memory.config import Config

pytestmark = pytest.mark.unit


def test_experience_example_config_assembles() -> None:
    """经验全套样例（双 profile / curator / abstractor skill / Selector）可装配。"""
    path = Path(__file__).resolve().parents[3] / "examples" / "config_experience.yml"
    api = assemble(config=Config.from_yaml(str(path)))
    try:
        assert api is not None, "经验样例配置应完成装配"
    finally:
        # 样例配 async_timer（周期任务要求）：close 组合释放调度器的后台循环，
        # 不清理会干扰同进程后续测试（http server 用例曾因此偶发失败）。
        api.close()
