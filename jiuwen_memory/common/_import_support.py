 # Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""导入容错支撑：插件/后端注册与启动期依赖引入的唯一入口。

装配各层时会 ``import_module`` 大量实现包以触发 ``@Producer.register`` 自注册。
其中不少实现依赖可选第三方包（Milvus / ES / openai / spacy …），单个包缺失只应
让**该插件**缺席，不该连坐同批次的其他插件，更不该阻断 ``MemoryAPI`` 实例构造。
故每个 ``import_module`` 调用都必须单独过一次 ``try/except ImportError``——本模块
把这套样板收敛为三个函数，各层直接引用，不再各自复制。

两类语义严格区分，不可混用：

- :func:`import_optional` —— **可选**依赖（插件实现包）。缺失记 warning 后继续，
  调用方据此得到"少一个后端"的降级结果。
- :func:`import_required` / :func:`import_required_attr` —— **必需**依赖（接入形态
  启动件、内核自身）。缺失同样先记 warning（带目标模块名，便于区分"可选依赖没装"
  与"实现模块内部导错名字"），再原样抛出：静默跳过会把配置错误伪装成功能缺失。

本模块**只依赖标准库**，是内核依赖图上的叶子；刻意不并入 ``common/_support.py``，
理由有二：

- **职责不同**：``_support.py`` 讲的是配置值语义（布尔归一、TLS 读取校验、scope
  命名空间、后端异常归一），本模块讲的是导入机制本身。
- **依赖重量不同**：``_support.py`` 会连带 ``type_def`` / ``Factory`` / ``errors``
  （实测引入 32 个项目模块），而本模块被 51 个 ``*_impl`` 包各 import 一次去触发自
  注册，本身应停在零项目依赖（实测连带 0 个）。

故本模块**不得**引入任何 ``jiuwen_memory`` 内部对象，以免装配期导入顺序反过来受
各层先后影响。
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["import_optional", "import_required", "import_required_attr"]


def _display_name(module_name: str, package: str | None) -> str:
    """相对导入时把 ``package`` 拼进日志，让 warning 指得出具体实现。"""
    if package and module_name.startswith("."):
        return f"{package}{module_name}"
    return module_name


def import_optional(module_name: str, package: str | None = None) -> None:
    """导入可选插件模块；失败只记录，不向上抛出。

    捕获范围限定 ``ImportError``：实现模块内部的其他异常（配置、语法、副作用）
    属于真实缺陷，必须照常冒泡，不能被当作"依赖未安装"吞掉。
    """
    target = _display_name(module_name, package)
    try:
        importlib.import_module(module_name, package)
    except ImportError as error:
        logger.warning("optional import skipped: %s: %s", target, error)


def import_required(module_name: str, package: str | None = None) -> ModuleType:
    """导入启动必需模块；失败记录缺失目标后原样抛出。"""
    target = _display_name(module_name, package)
    try:
        return importlib.import_module(module_name, package)
    except ImportError as error:
        logger.warning("required import failed: %s: %s", target, error)
        raise


def import_required_attr(module_name: str, attribute: str) -> Any:
    """取必需模块的某个属性（等价于 ``import_module(name).attr``）。"""
    return getattr(import_required(module_name), attribute)
