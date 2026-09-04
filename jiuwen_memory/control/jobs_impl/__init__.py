# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""控制层任务实现包（架构 §8）。

各 Job 实现自本包导出：``EvolveJob``（通用演进任务，mode 由构造参数注入）、
``MiddleToLongJob``（中期转长期专用，见 §2.3）。

导入本包即触发 ``@JobFactoryProducer.register("default")`` 装饰的
``_build_job_factory``——把各 Job 类型的 Spec builder 注册到 JobFactory，
供 :meth:`JobFactoryProducer.build_named` 装配时取用。
"""

from __future__ import annotations

# 触发 @JobFactoryProducer.register 装饰器，注册各 Spec 的 with_scope 方法。
from . import middle_to_long_job as middle_to_long_job  # noqa: F401
