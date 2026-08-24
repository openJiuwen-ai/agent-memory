# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""注册引导：import 各检索算子实现包，触发其 ``@Producer.register`` 自注册。

工厂句柄（如 :class:`~retrieval.recaller.RecallerProducer`）定义在**接口模块**，消费方据此
只依赖接口层；但实现的注册仍发生在 *import 实现模块* 时。本模块把「import 全部实现」收敛到
一处显式调用，由装配入口 ``build_kernel`` 在组装前调用一次，
保证注册表已填满。与 :func:`storage.bootstrap.register_backends` 同构。
"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_operators() -> None:
    """import 各检索算子实现包，完成所有实现的自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("jiuwen_memory.retrieval.discloser_impl")
    import_module("jiuwen_memory.retrieval.fuser_impl")
    import_module("jiuwen_memory.retrieval.query_parser_impl")
    import_module("jiuwen_memory.retrieval.recaller_impl")
    import_module("jiuwen_memory.retrieval.retriever_impl")
    _REGISTERED = True
