"""pipeline_impl 实现集：触发 MemoryPipeline 实现自注册。"""

from __future__ import annotations

from importlib import import_module

import_module(".metadata_pipeline", __name__)
