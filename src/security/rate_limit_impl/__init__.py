"""rate_limit_impl 实现集：工厂 RateLimitProducer + 各实现。

import 各实现模块即触发其 ``@RateLimitProducer.register(...)`` 自注册；
本包只对外暴露工厂 RateLimitProducer。
"""

from importlib import import_module

from security.rate_limit import RateLimitProducer

import_module(".token_bucket_limiter", __name__)
import_module(".unlimited_limiter", __name__)

__all__ = ["RateLimitProducer"]
