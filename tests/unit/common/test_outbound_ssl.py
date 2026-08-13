"""出站 HTTP 客户端（LLM / Embedder / Reranker）的 SSL 配置测试。

三者共用 `<prefix>_ssl_verify` / `<prefix>_ssl_ca_cert` 两个全局参数，语义与 storage
侧对齐，唯一差异是 `ssl_verify=true` 但缺证书时**放行**（回落系统 CA）——公网端点由
公共 CA 签发是正常状态，而 storage 连的托管实例一律私有 CA，缺证书必然失败。

覆盖：布尔归一、默认不干预、开启后强制 https、证书文件缺失拦截，以及各组件是否
把 verify 真正传给了 httpx。
"""

from __future__ import annotations

from typing import Any

import openai
import pytest

from jiuwen_memory.common._support import (
    as_bool,
    outbound_verify,
    read_outbound_ssl,
    require_ca_file,
    require_https,
)
from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.embedder.base import EmbedderProducer
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.reranker.base import RerankerProducer
from jiuwen_memory.config import AssemblyContext

pytestmark = pytest.mark.unit

register_plugins()


# -- 配置读取 ---------------------------------------------------------------- #


def test_outbound_ssl_is_disabled_by_default() -> None:
    """不配置时完全不干预：http 明文直连（开发自测），https 走 SDK 默认校验。"""
    ssl = read_outbound_ssl({}, "llm")
    assert ssl.verify is False
    assert ssl.ca_cert is None


def test_prefix_isolates_components() -> None:
    """三个组件各读各的前缀，互不串台。"""
    config = {"llm_ssl_verify": "true", "embedder_ssl_verify": "false"}
    assert read_outbound_ssl(config, "llm").verify is True
    assert read_outbound_ssl(config, "embedder").verify is False
    assert read_outbound_ssl(config, "reranker").verify is False


def test_blank_ca_cert_is_normalized_to_none() -> None:
    """``${VAR:-}`` 展开出空串，须归一为 None 而非空路径。"""
    assert read_outbound_ssl({"llm_ssl_ca_cert": "  "}, "llm").ca_cert is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("FALSE", False), ("0", False), ("", False), (None, False),
     ("true", True), ("on", True), ("1", True), (True, True)],
)
def test_as_bool_normalizes_string_config_values(value: Any, expected: bool) -> None:
    assert as_bool(value, default=False) is expected


# -- verify 取值翻译 --------------------------------------------------------- #


@pytest.mark.parametrize("ca_cert", [None, "", "   "])
def test_verify_falls_back_to_system_ca_without_cert(ca_cert: Any) -> None:
    """缺证书回落 True（系统 CA）而非报错——公网端点走公共 CA 是正常状态。"""
    assert outbound_verify(ca_cert) is True


def test_verify_uses_cert_path_when_given(tmp_path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("x", encoding="utf-8")
    ssl = read_outbound_ssl(
        {"llm_ssl_verify": "true", "llm_ssl_ca_cert": str(cert)}, "llm"
    )
    assert outbound_verify(ssl.ca_cert) == str(cert)


# -- 装配期校验 -------------------------------------------------------------- #


@pytest.mark.parametrize("base_url", ["http://m:8000/v1", "ftp://m/v1", "m:8000/v1"])
def test_require_https_rejects_non_https(base_url: str) -> None:
    with pytest.raises(ValidationError, match="https://"):
        require_https(base_url, component="c", param="llm")


@pytest.mark.parametrize("base_url", ["", None, "   "])
def test_require_https_allows_empty_base_url(base_url: Any) -> None:
    """未配置 base_url → 走 SDK 内置端点，官方 API 均为 https。"""
    require_https(base_url, component="c", param="llm")


def test_require_ca_file_rejects_missing_path() -> None:
    """httpx 构造时立即加载证书，路径错只抛不带上下文的 FileNotFoundError。"""
    with pytest.raises(ValidationError, match="不存在"):
        require_ca_file("/nope/ca.pem", component="c", param="llm")


def test_require_ca_file_allows_absent_cert() -> None:
    require_ca_file(None, component="c", param="llm")


# -- 各组件的装配行为 --------------------------------------------------------- #


_HTTP = "http://m:8000/v1"
_HTTPS = "https://m/v1"


@pytest.mark.parametrize(
    ("producer", "target", "prefix", "extra"),
    [
        (LlmProducer, "openai", "llm", {"llm_api_key": "k"}),
        (LlmProducer, "dashscope", "llm", {"llm_api_key": "k"}),
        (EmbedderProducer, "openai", "embedder", {"embedder_api_key": "k"}),
        (RerankerProducer, "api", "reranker", {}),
    ],
)
class TestComponentAssembly:
    @staticmethod
    def test_plaintext_allowed_when_disabled(
        producer: Any, target: str, prefix: str, extra: dict
    ) -> None:
        """默认关闭时 http 直连可用——开发自测场景。"""
        producer.build(target, {**extra, f"{prefix}_base_url": _HTTP}, AssemblyContext())

    @staticmethod
    def test_plaintext_rejected_when_enabled(
        producer: Any, target: str, prefix: str, extra: dict
    ) -> None:
        with pytest.raises(ValidationError, match="https://"):
            producer.build(
                target,
                {**extra, f"{prefix}_base_url": _HTTP, f"{prefix}_ssl_verify": "true"},
                AssemblyContext(),
            )

    @staticmethod
    def test_https_without_cert_allowed(
        producer: Any, target: str, prefix: str, extra: dict
    ) -> None:
        """与 storage 侧的关键差异：缺证书放行，回落系统 CA。"""
        producer.build(
            target,
            {**extra, f"{prefix}_base_url": _HTTPS, f"{prefix}_ssl_verify": "true"},
            AssemblyContext(),
        )

    @staticmethod
    def test_missing_cert_file_rejected(
        producer: Any, target: str, prefix: str, extra: dict
    ) -> None:
        with pytest.raises(ValidationError, match="不存在"):
            producer.build(
                target,
                {
                    **extra,
                    f"{prefix}_base_url": _HTTPS,
                    f"{prefix}_ssl_verify": "true",
                    f"{prefix}_ssl_ca_cert": "/nope/ca.pem",
                },
                AssemblyContext(),
            )


# -- verify 是否真正抵达 httpx ------------------------------------------------ #


@pytest.mark.parametrize(
    ("producer", "prefix", "extra"),
    [
        (LlmProducer, "llm", {"llm_api_key": "k"}),
        (EmbedderProducer, "embedder", {"embedder_api_key": "k"}),
    ],
)
def test_openai_components_keep_sdk_default_http_client(
    monkeypatch: pytest.MonkeyPatch,
    producer: Any,
    prefix: str,
    extra: dict,
) -> None:
    """自定义信任锚须使用 SDK 客户端，不能退化为裸 httpx.Client 默认参数。"""
    recorded: dict[str, Any] = {}
    sdk_http_client = object()

    def fake_default_httpx_client(**kwargs: Any) -> object:
        recorded["http_client_kwargs"] = kwargs
        return sdk_http_client

    def fake_openai(**kwargs: Any) -> object:
        recorded["openai_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(openai, "DefaultHttpxClient", fake_default_httpx_client)
    monkeypatch.setattr(openai, "OpenAI", fake_openai)

    component = producer.build(
        "openai",
        {
            **extra,
            f"{prefix}_base_url": _HTTPS,
            f"{prefix}_ssl_verify": "true",
        },
        AssemblyContext(),
    )
    # LLM / Embedder 均惰性建连；访问 .client 才触发 DefaultHttpxClient
    component.client

    assert recorded["http_client_kwargs"] == {"verify": True}
    assert recorded["openai_kwargs"]["http_client"] is sdk_http_client
    assert recorded["openai_kwargs"]["base_url"] == _HTTPS


def test_reranker_passes_verify_to_httpx(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("x", encoding="utf-8")
    recorded: dict[str, Any] = {}

    import httpx

    def fake_client(**kwargs: Any) -> object:
        recorded.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "Client", fake_client)
    RerankerProducer.build(
        "api",
        {
            "reranker_base_url": _HTTPS,
            "reranker_ssl_verify": "true",
            "reranker_ssl_ca_cert": str(cert),
        },
        AssemblyContext(),
    )

    assert recorded["verify"] == str(cert)


def test_reranker_omits_verify_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭时不传 verify，保持 httpx 默认行为不被干预。"""
    recorded: dict[str, Any] = {}

    import httpx

    def fake_client(**kwargs: Any) -> object:
        recorded.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "Client", fake_client)
    RerankerProducer.build("api", {"reranker_base_url": _HTTP}, AssemblyContext())

    assert "verify" not in recorded
