"""注册引导：import 各存储实现包，触发其 ``@Producer.register`` 自注册。

工厂句柄（如 :class:`~storage.kv.KvProducer`）定义在**接口模块**，消费方据此只依赖
接口层；但实现的注册仍发生在 *import 实现模块* 时。本模块把「import 全部实现」收敛到
一处显式调用，由装配入口 ``build_kernel`` 在组装前调用一次，
保证注册表已填满——避免「从接口拿到 Producer 句柄，注册表却是空的」。

第三方后端（redis / postgres / milvus / pgvector / elasticsearch / nano-graphrag）的
重依赖均为惰性导入，仅在实例化/访问时才需就绪，故 import 实现模块本身始终安全。
"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_backends() -> None:
    """import 各存储实现包，完成所有后端的自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("jiuwen_memory.storage.kv_impl")
    import_module("jiuwen_memory.storage.vector_impl")
    import_module("jiuwen_memory.storage.graph_impl")
    import_module("jiuwen_memory.storage.fulltext_impl")
    import_module("jiuwen_memory.storage.fusion_impl")
    import_module("jiuwen_memory.storage.fs_impl")
    import_module("jiuwen_memory.storage.storage_impl")
    _REGISTERED = True
