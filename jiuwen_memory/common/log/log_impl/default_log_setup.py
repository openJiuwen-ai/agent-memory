# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""默认日志初始化实现——注册为 ``"default"``。

按配置树节点（``memory_api.params``）的 log_level / log_format / log_datefmt / log_file
配置 ``agent-memory`` 根 logger：添加 StreamHandler + Formatter，设置级别，阻断向上传播。
"""

from __future__ import annotations

from jiuwen_memory.common.log.base import setup_logging

from .log_producer import LogProducer

# -- 注册到 LogProducer（实现自注册，新增无需改 producer/build_kernel） ------ #


@LogProducer.register("default")
def _build(config):
    """Builder: 按 Config 执行默认日志初始化。"""
    setup_logging(config)
