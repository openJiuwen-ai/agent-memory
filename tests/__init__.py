"""测试包引导。

导入本包时把仓库根挂上 sys.path，使 ``python3 -m unittest discover -s tests``
可直接跑（与 ``pytest`` 的 ``pythonpath = ["."]`` 一致）。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
