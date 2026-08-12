"""统一日志接口——横切共用基础设施（架构 §13 侧，不可变配置）。

全系统通过 ``get_logger(name)`` 获取带 ``agent-memory.`` 前缀的层级 logger，
统一命名空间、统一初始化、统一格式。与 :mod:`common.audit` 类似，日志是
横切基础设施而非可插拔计算能力，因此不继承 Plugin、不进 PluginType。

使用方式::

    from jiuwen_memory.common.log import get_logger
    logger = get_logger(__name__)      # → "agent_memory.common.embedder.embedder_impl.bge_m3"
    logger.info("model loaded")

初始化在 :func:`api.assemble` 入口调用 :func:`setup_logging` 完成，
无需各模块单独配置——一次装配、全局生效。
"""

from __future__ import annotations

import logging
import os

_LOG_PREFIX = "agent_memory"


def get_logger(name: str) -> logging.Logger:
    """获取统一命名的 logger：``agent-memory.<name>``。

    ``name`` 通常传 ``__name__``（模块全限定名），产出层级清晰的 logger 名：
    ``agent_memory.common.embedder.embedder_impl.bge_m3_embedder`` 等。
    """
    # 去掉包根前缀，保持 logger 名为 agent_memory.<子模块>...
    clean = name.removeprefix("jiuwen_memory.").removeprefix("src.")
    full_name = f"{_LOG_PREFIX}.{clean}" if clean else _LOG_PREFIX
    return logging.getLogger(full_name)


def setup_logging(config=None) -> None:
    """按配置树节点初始化 ``agent-memory`` 根 logger（格式、级别、handler）。

    ``config`` 是装配配置树节点（``ComponentSpec``，经各 ``_build`` 传阅的同一对象）：
    日志参数 ``log_level`` / ``log_format`` / ``log_datefmt`` / ``log_file`` 写在
    ``memory_api.params``，经 :meth:`ComponentSpec.get` 沿父链回退读取——与
    ``vector_enabled`` / ``embedder_dim`` 等跨切面参数同路。``None`` 时用默认值
    （INFO 级别 + 标准格式）。

    在 :func:`api.assemble` / :func:`api.build_kernel` 入口调用一次即可；
    后续各模块 ``get_logger`` 产出的子 logger 自动继承根 logger 配置。

    Handler 策略：
      - ``log_file`` 为空：仅 StreamHandler（终端 stderr 输出）
      - ``log_file`` 非空：StreamHandler + FileHandler（同时终端 + 落盘）
    """
    level_name = config.get("log_level", "INFO") if config is not None else "INFO"
    fmt = (
        config.get("log_format", "[%(asctime)s] %(name)s %(levelname)s %(message)s")
        if config is not None
        else "[%(asctime)s] %(name)s %(levelname)s %(message)s"
    )
    datefmt = config.get("log_datefmt", "%Y-%m-%d %H:%M:%S") if config is not None else "%Y-%m-%d %H:%M:%S"
    log_file = config.get("log_file", "") if config is not None else ""

    level = logging.getLevelName(level_name)
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    root_logger = logging.getLogger(_LOG_PREFIX)
    root_logger.setLevel(level)

    # 只在尚未配置 handler 时添加，避免重复 append（多次 assemble 场景）
    if not root_logger.handlers:
        # 终端输出（始终启用）
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

        # 文件落盘（log_file 非空时启用；自动创建目录）
        if log_file:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)

    # 阻断向上传播到 Python 全局 root logger（避免第三方库日志混入）
    root_logger.propagate = False
