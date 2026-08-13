"""分布式锁横切接口：跨实例互斥原语。"""

from .lock import (
    DEFAULT_LEASE_MS,
    DEFAULT_WAIT_TIMEOUT_MS,
    KEY_PREFIX,
    LockError,
    LockHandle,
    LockLostError,
    LockProducer,
    LockProvider,
    LockTimeoutError,
)

__all__ = [
    "DEFAULT_LEASE_MS",
    "DEFAULT_WAIT_TIMEOUT_MS",
    "KEY_PREFIX",
    "LockError",
    "LockHandle",
    "LockLostError",
    "LockProducer",
    "LockProvider",
    "LockTimeoutError",
]
