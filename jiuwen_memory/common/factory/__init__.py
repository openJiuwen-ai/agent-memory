# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""注册式工厂层：:class:`Factory` 基类。

各组件的工厂均为组件级注册式工厂，且**与各自的契约同处接口模块**（如
:class:`~storage.kv.KvProducer` 在 ``storage.kv``、:class:`~retrieval.recaller.RecallerProducer`
在 ``retrieval.recaller``、:class:`~common.tokenizer.base.TokenizerProducer` 在
``common.tokenizer.base``）；消费方只依赖接口层即可取实例。实现的注册发生在 import 实现模块时，
由各层 ``<layer>.bootstrap.register_*`` 在装配入口统一触发；本包只保留通用注册式基类。
跨组件的装配编排在 ``api.build_kernel``。
"""

from .factory import Factory

__all__ = ["Factory"]
