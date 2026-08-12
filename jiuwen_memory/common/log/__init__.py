"""统一日志功能：横切基础设施，为全系统提供层级 logger 获取与统一初始化。

调用方只需::

    from jiuwen_memory.common.log import get_logger
    logger = get_logger(__name__)

初始化在 ``api.assemble`` 入口自动完成，无需各模块单独配置。
"""

from .base import get_logger, setup_logging

__all__ = ["get_logger", "setup_logging"]
