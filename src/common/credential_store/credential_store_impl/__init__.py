"""凭据存储实现集。"""

from importlib import import_module

from common.credential_store.base import KeyStoreProducer

import_module(".memory_key_store", __name__)

__all__ = ["KeyStoreProducer"]
