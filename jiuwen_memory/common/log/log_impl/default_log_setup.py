# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""默认日志初始化实现——注册为 ``"default"``。

按配置 ``globals`` 的日志项初始化 SDK 日志路由；默认输出到 stderr，并阻断 SDK
命名空间向 Python 根 logger 传播。
"""

from __future__ import annotations

from jiuwen_memory.common.log.base import setup_logging

from .log_producer import LogProducer

# -- 注册到 LogProducer（实现自注册，新增无需改 producer/build_kernel） ------ #


@LogProducer.register("default")
def _build(config):
    """Builder: 按 Config 执行默认日志初始化。"""
    setup_logging(config)
