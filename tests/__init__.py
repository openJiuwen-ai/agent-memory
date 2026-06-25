"""检索层测试包。

零外部依赖（标准库 unittest）。导入本包时把 ``src/`` 挂上 sys.path，使
``python3 -m unittest discover -s tests`` 可直接跑，无需安装或设 PYTHONPATH。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.append(_SRC)
