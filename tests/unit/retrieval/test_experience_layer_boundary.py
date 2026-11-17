"""分层边界（F08）：检索层不导入构建层。

经验记忆的共用部件（效用记录、质量分、策略取值）下沉在
``common/experience.py``，正是为了让检索侧不依赖构建侧——两侧的公共下游是
common。本文件从源码与运行时两个角度固定这条边界：任何 retrieval 模块引用
``jiuwen_memory.construction`` 即失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import jiuwen_memory.retrieval

pytestmark = pytest.mark.unit

RETRIEVAL_ROOT = Path(jiuwen_memory.retrieval.__file__).parent


def test_retrieval_sources_never_import_construction() -> None:
    """源码扫描：retrieval 包下任何 .py 不得引用 jiuwen_memory.construction。"""
    offenders = []
    for py_file in sorted(RETRIEVAL_ROOT.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if "jiuwen_memory.construction" in text:
            offenders.append(py_file.relative_to(RETRIEVAL_ROOT).as_posix())
    assert offenders == [], (
        f"检索层不得导入构建层（F08 分层边界）：{offenders} 引用了 jiuwen_memory.construction"
    )


def test_importing_selector_does_not_pull_construction() -> None:
    """运行时：import 选取算子实现不应连带加载任何构建层模块。"""
    before = {name for name in sys.modules if name.startswith("jiuwen_memory.construction")}
    # ``as`` 别名避免函数内重新绑定 ``jiuwen_memory``（G.VAR.03）；导入副作用
    # （sys.modules 变化）与裸 import 完全一致，被测行为不变。
    import jiuwen_memory.retrieval.selector_impl.experience_selector as _selector  # noqa: F401

    after = {name for name in sys.modules if name.startswith("jiuwen_memory.construction")}
    assert after <= before, (
        f"import 选取算子引入了构建层模块：{sorted(after - before)}"
    )
