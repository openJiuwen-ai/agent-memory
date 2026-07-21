# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for SM4StorageCodec."""

import pytest
from jiuwen_memory.foundation.codec.sm4_storage_codec import SM4StorageCodec


class TestSM4StorageCodec:
    """Test suite for SM4-128-CBC storage codec."""

    def test_valid_key_initialization(self):
        """Test that a valid 16-byte key initializes successfully."""
        key = b'0123456789abcdef'  # 16 bytes
        codec = SM4StorageCodec(key=key)
        assert codec._key == key

    def test_invalid_key_length(self):
        """Test that invalid key lengths raise ValueError."""
        # Too short
        with pytest.raises(ValueError, match="SM4 key must be 16 bytes"):
            SM4StorageCodec(key=b'short')
        
        # Too long
        with pytest.raises(ValueError, match="SM4 key must be 16 bytes"):
            SM4StorageCodec(key=b'0123456789abcdef0123456789abcdef')  # 32 bytes

    def test_empty_key_passthrough(self):
        """Test that empty key returns plaintext without encryption."""
        codec = SM4StorageCodec(key=b'')
        plaintext = "Hello, World!"
        
        assert codec.encode(plaintext) == plaintext
        assert codec.decode(plaintext) == plaintext

    def test_empty_text_passthrough(self):
        """Test that empty text returns as-is."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        assert codec.encode("") == ""
        assert codec.decode("") == ""

    def test_encode_decode_roundtrip(self):
        """Test that encode followed by decode returns original text."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        test_cases = [
            "Hello, World!",
            "你好，世界！",
            "Mixed content: 中文 and English 12345!@#",
            "A" * 100,  # Long text
            "Special chars: \n\t\r\\\"'",
        ]
        
        for text in test_cases:
            encoded = codec.encode(text)
            decoded = codec.decode(encoded)
            assert decoded == text, f"Failed for: {text[:50]}..."
            # Verify ciphertext is different from plaintext
            assert encoded != text, "Ciphertext should differ from plaintext"

    def test_different_plaintexts_produce_different_ciphertexts(self):
        """Test that same plaintext encrypts to different ciphertexts (due to random IV)."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        plaintext = "Test message"
        
        ciphertext1 = codec.encode(plaintext)
        ciphertext2 = codec.encode(plaintext)
        
        # Both should decrypt to the same plaintext
        assert codec.decode(ciphertext1) == plaintext
        assert codec.decode(ciphertext2) == plaintext
        
        # But ciphertexts should be different (random IV)
        assert ciphertext1 != ciphertext2

    def test_invalid_ciphertext_handling(self):
        """Test that invalid ciphertext returns original data."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        # Too short (less than IV length)
        invalid_data = "abc"
        result = codec.decode(invalid_data)
        assert result == invalid_data  # Should return original on error

    def test_chinese_text_encoding(self):
        """Test proper UTF-8 encoding for Chinese characters."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        chinese_text = "这是一段中文测试文本，包含特殊字符：！@#￥%……&*（）"
        encoded = codec.encode(chinese_text)
        decoded = codec.decode(encoded)
        
        assert decoded == chinese_text

    def test_multiline_text(self):
        """Test encoding/decoding of multiline text."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        multiline = """Line 1
Line 2
Line 3 with 中文"""
        
        encoded = codec.encode(multiline)
        decoded = codec.decode(encoded)
        
        assert decoded == multiline

    def test_key_isolation(self):
        """Test that different keys cannot decrypt each other's ciphertext."""
        key1 = b'0123456789abcdef'
        key2 = b'fedcba9876543210'
        
        codec1 = SM4StorageCodec(key=key1)
        codec2 = SM4StorageCodec(key=key2)
        
        plaintext = "Secret message"
        ciphertext1 = codec1.encode(plaintext)
        
        # Decrypting with wrong key should fail and return original ciphertext
        result = codec2.decode(ciphertext1)
        assert result == ciphertext1  # Returns original on decryption failure

    def test_unicode_edge_cases(self):
        """Test various Unicode edge cases."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        test_cases = [
            "Emoji: 😀🎉🚀",
            "Mixed: Hello 你好 🌍",
            "Rare chars: 𡈽",  # Rare CJK characters
        ]
        
        for text in test_cases:
            encoded = codec.encode(text)
            decoded = codec.decode(encoded)
            assert decoded == text

    def test_codec_interface_compliance(self):
        """Test that SM4StorageCodec implements the StorageCodec protocol."""
        from jiuwen_memory.foundation.codec.storage_codec import StorageCodec
        
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        assert isinstance(codec, StorageCodec)

    def test_padding_edge_cases(self):
        """Test PKCS7 padding with various data lengths."""
        key = b'0123456789abcdef'
        codec = SM4StorageCodec(key=key)
        
        # Test lengths: 0, 1, 15, 16, 17, 31, 32, 33
        for length in [0, 1, 15, 16, 17, 31, 32, 33]:
            text = "A" * length if length > 0 else ""
            encoded = codec.encode(text)
            decoded = codec.decode(encoded)
            assert decoded == text, f"Failed for length {length}"
