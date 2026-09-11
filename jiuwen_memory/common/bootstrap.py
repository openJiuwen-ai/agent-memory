# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""注册引导：import 各共享组件实现包，触发其 ``@Producer.register`` 自注册。

工厂句柄定义在各组件的接口模块（多数插件为 ``common.<plugin>.base``，横切组件
security / lock 为 ``common.<name>.<name>``），消费方只依赖接口层；实现的注册
发生在 import 实现模块时，由本函数在装配入口统一触发。与各层 bootstrap 同构。
"""

from __future__ import annotations

import logging
from importlib import import_module

_REGISTERED = False


def register_plugins() -> None:
    """import 各共享插件实现包，完成自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        import_module("jiuwen_memory.common.tokenizer.tokenizer_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.normalizer.normalizer_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.embedder.embedder_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.chunker.chunker_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.feature_extractor.feature_extractor_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.reranker.reranker_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.llm.llm_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.audit.audit_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.security.authentication_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.security.security_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.common.lock.lock_impl")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    _REGISTERED = True
