# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SM4 storage codec implementing the StorageCodec protocol.

Provides encryption/decryption using the Chinese National Standard SM4 
algorithm (CBC mode with PKCS7 padding) for memory storage security.

Usage example::

    from jiuwen_memory.foundation.codec import register_storage_codec
    
    sm4_key = b'0123456789abcdef'  # Must be 16 bytes (128-bit)
    register_storage_codec("sm4", SM4StorageCodec(key=sm4_key))
    
    config = MemoryEngineConfig(crypto_key=b"", codec="sm4")
"""

import secrets
from gmssl import sm4

from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType


SM4_KEY_LENGTH = 16
IV_LENGTH = 16


class SM4StorageCodec:
    """SM4-128-CBC storage codec implementing the StorageCodec protocol.

    Injected into :class:`BaseMemoryIndex` subclasses via
    :meth:`BaseMemoryIndex.set_storage_codec` to transparently
    encrypt/decrypt the ``text`` field of memory documents.

    When ``key`` is empty, ``encode`` and ``decode`` pass through
    as plain text, matching the no-codec default behavior.

    Encryption format: ``IV_hex + ciphertext_hex``
    - IV: 16 bytes (32 hex chars)
    - Ciphertext: variable length, hex encoded
    """

    def __init__(self, key: bytes):
        """Initialize SM4 codec with a 16-byte (128-bit) key.

        Args:
            key: SM4 encryption key, must be exactly 16 bytes.

        Raises:
            ValueError: If key length is not 16 bytes.
        """
        self._key = key
        if self._key and len(self._key) != SM4_KEY_LENGTH:
            raise ValueError(
                f"SM4 key must be {SM4_KEY_LENGTH} bytes, got {len(self._key)}"
            )

    @staticmethod
    def _pkcs7_pad(data: bytes, block_size: int = SM4_KEY_LENGTH) -> bytes:
        """Apply PKCS7 padding to data.

        Args:
            data: Raw bytes to pad.
            block_size: Block size (default 16 for SM4).

        Returns:
            Padded bytes.
        """
        padding_len = block_size - (len(data) % block_size)
        return data + bytes([padding_len] * padding_len)

    @staticmethod
    def _pkcs7_unpad(data: bytes, block_size: int = SM4_KEY_LENGTH) -> bytes:
        """Remove PKCS7 padding from data.

        Args:
            data: Padded bytes.
            block_size: Block size (default 16 for SM4).

        Returns:
            Unpadded bytes.

        Raises:
            ValueError: If padding is invalid.
        """
        if not data:
            raise ValueError("Cannot unpad empty data")
        padding_len = data[-1]
        if padding_len < 1 or padding_len > block_size:
            raise ValueError(f"Invalid padding length: {padding_len}")
        if data[-padding_len:] != bytes([padding_len] * padding_len):
            raise ValueError("Invalid PKCS7 padding")
        return data[:-padding_len]

    def encode(self, text: str) -> str:
        """Encrypt plaintext using SM4-CBC mode.

        Args:
            text: Plaintext string to encrypt.

        Returns:
            Encrypted string in hex format (IV + ciphertext), or original
            text if key is empty or encryption fails.
        """
        if not self._key or not text:
            return text

        try:
            # Generate random IV
            iv = secrets.token_bytes(IV_LENGTH)

            # Pad plaintext
            plaintext_bytes = text.encode('utf-8')
            padded_text = self._pkcs7_pad(plaintext_bytes)

            # Encrypt with SM4-CBC
            crypt_sm4 = sm4.CryptSM4()
            crypt_sm4.set_key(self._key, sm4.SM4_ENCRYPT)
            ciphertext = crypt_sm4.crypt_cbc(iv, padded_text)

            # Format: IV_hex + ciphertext_hex
            return iv.hex() + ciphertext.hex()

        except Exception as e:
            memory_logger.warning(
                "Encrypt error via SM4 codec",
                exception=str(e),
                event_type=LogEventType.MEMORY_PROCESS,
            )
            return text

    def decode(self, data: str) -> str:
        """Decrypt ciphertext using SM4-CBC mode.

        Args:
            data: Encrypted string in hex format (IV + ciphertext).

        Returns:
            Decrypted plaintext string, or original data if key is empty
            or decryption fails.
        """
        if not self._key or not data:
            return data

        try:
            # Parse IV and ciphertext
            iv_hex = data[:IV_LENGTH * 2]  # First 32 hex chars
            ciphertext_hex = data[IV_LENGTH * 2:]  # Rest

            if len(iv_hex) != IV_LENGTH * 2:
                raise ValueError(
                    f"Ciphertext too short: expected at least "
                    f"{IV_LENGTH * 2} hex chars for IV, got {len(iv_hex)}"
                )

            iv = bytes.fromhex(iv_hex)
            ciphertext = bytes.fromhex(ciphertext_hex)

            # Decrypt with SM4-CBC
            crypt_sm4 = sm4.CryptSM4()
            crypt_sm4.set_key(self._key, sm4.SM4_DECRYPT)
            decrypted_padded = crypt_sm4.crypt_cbc(iv, ciphertext)

            # Remove padding
            decrypted = self._pkcs7_unpad(decrypted_padded)

            return decrypted.decode('utf-8')

        except Exception as e:
            memory_logger.warning(
                "Decrypt error via SM4 codec",
                exception=str(e),
                event_type=LogEventType.MEMORY_PROCESS,
            )
            return data
