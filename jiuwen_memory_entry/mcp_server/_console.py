# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``agent-memory-mcp`` 的 console_scripts 入口。

``mcp_server.__main__`` 在模块级就要求 mcp SDK，缺依赖时抛 ``RuntimeError``。
这是内核侧的 fail-closed 语义，库调用方需要它；但对命令调用方，直接抛出会让
用户看到整段 traceback。本模块把该异常转成一行可执行提示 + 退出码 2：

    $ agent-memory-mcp
    agent-memory-mcp: MCP surface 需要 mcp SDK：pip install ".[mcp]"
    $ echo $?
    2

本模块不导入 ``mcp_server.__main__`` 的模块级代码（在 ``main()`` 内延迟导入），
因此未装 extra 时也能被 console_scripts 加载。

导入期只过滤内核侧那条重复的 ``required import failed`` 日志，不改动 logger
级别——``development authentication is enabled`` 等安全告警必须照常输出。
"""

from __future__ import annotations

import logging
import sys

from jiuwen_memory.api import ValidationError

# 内核侧在抛 RuntimeError 前会 warning 一行原始 ImportError；下面会给出更可执行的
# 提示，这行属于重复噪音。按消息前缀精确过滤，不碰级别。
_REDUNDANT_IMPORT_WARNING = "required import failed"


class _DropRedundantImportWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(_REDUNDANT_IMPORT_WARNING)


def main() -> int:
    """转发到 MCP 入口；配置或依赖问题给出可执行提示而非 traceback。"""
    kernel_logger = logging.getLogger("agent-memory.mcp")
    noise_filter = _DropRedundantImportWarning()
    kernel_logger.addFilter(noise_filter)
    try:
        from jiuwen_memory_entry.mcp_server.__main__ import main as serve
    except RuntimeError as exc:
        # 缺依赖是用户可自行修复的配置问题，不是程序缺陷：不给 traceback。
        print(f"agent-memory-mcp: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        # 导入期装配失败（如 JIUWEN_MEMORY_MCP_AUTH_MODE 非法）同属配置问题。
        print(f"agent-memory-mcp: {exc}", file=sys.stderr)
        return 2
    finally:
        # 只影响本次导入；服务运行期的日志不受影响。
        kernel_logger.removeFilter(noise_filter)
    return serve()


if __name__ == "__main__":
    sys.exit(main())
