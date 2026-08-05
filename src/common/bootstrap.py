"""注册引导：import 各共享组件实现包，触发其 ``@Producer.register`` 自注册。

工厂句柄定义在各组件的接口模块（统一为 ``common.<capability>.base``），
消费方只依赖接口层；实现的注册
发生在 import 实现模块时，由本函数在装配入口统一触发。与各层 bootstrap 同构。
"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_plugins() -> None:
    """import 各共享插件实现包，完成自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("common.tokenizer.tokenizer_impl")
    import_module("common.normalizer.normalizer_impl")
    import_module("common.embedder.embedder_impl")
    import_module("common.chunker.chunker_impl")
    import_module("common.feature_extractor.feature_extractor_impl")
    import_module("common.reranker.reranker_impl")
    import_module("common.llm.llm_impl")
    import_module("common.audit.audit_impl")
    import_module("common.authentication.authentication_impl")
    import_module("common.credential_store.credential_store_impl")
    import_module("common.admission.admission_impl")
    import_module("common.encryption.encryption_impl")
    _REGISTERED = True
