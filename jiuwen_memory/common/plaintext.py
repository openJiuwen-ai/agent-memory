# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Internal plaintext refusal, propagated through the existing BackendError contract."""

from jiuwen_memory.common.errors import BackendError


class PlaintextDataError(BackendError):
    """Internal classification; callers continue to catch the existing BackendError."""

    def __init__(self) -> None:
        super().__init__("plaintext data requires explicit migration or clearing")


def require_ciphertext(provider, value: bytes) -> None:
    # A missing header is not proof of legacy plaintext. This refuses it without repair.
    # An ENC1 header still requires authentication by the configured provider.
    if provider.is_ciphertext(value) is False:
        raise PlaintextDataError()
