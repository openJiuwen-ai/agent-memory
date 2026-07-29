"""fs_impl 实现集：工厂 FsProducer + 各实现。

import 各实现模块即触发其 ``@FsProducer.register(...)`` 自注册；本包只对外暴露工厂 FsProducer。
"""

from importlib import import_module

from storage.fs import FsProducer

import_module(".in_memory_fs_store", __name__)
import_module(".local_fs", __name__)
import_module(".encrypted_fs_store", __name__)

__all__ = ["FsProducer"]
