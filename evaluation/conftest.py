"""pytest 引导：把仓库根与 ``src`` 接入 ``sys.path``，使 evaluation/ 下的冒烟
测试既能 ``import evaluation.*`` 也能 ``import api/common/...``（src 布局）。

默认 ``pytest`` 只收集 ``tests/``（见 pyproject ``testpaths``），评测冒烟需显式
``pytest evaluation/smoke_test`` 触发——此时本 conftest 被加载、完成路径接线。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根 = evaluation 的上一级
for _path in (os.path.join(_ROOT, "src"), _ROOT):
    if _path not in sys.path:
        sys.path.append(_path)
