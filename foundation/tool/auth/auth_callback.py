# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from abc import ABC, abstractmethod
from enum import Enum
from typing import AsyncGenerator, Dict, Type, Optional
import aiohttp
import httpx
from common.logging import logger
from common.security.ssl_utils import SslUtils
from foundation.tool.auth.auth import ToolAuthConfig, ToolAuthResult


class AuthType(Enum):
    SSL = "ssl"
    HEADER_AND_QUERY = "header_and_query"


class AuthStrategy(ABC):
    """Base class for authentication strategies"""
    auth_type: AuthType  # Subclasses must define this attribute

    @abstractmethod
    async def authenticate(self, auth_config: ToolAuthConfig, **kwargs) -> ToolAuthResult:
        pass


class SSLAuthStrategy(AuthStrategy):
    """SSL authentication strategy"""
    auth_type = AuthType.SSL

    async def authenticate(self, auth_config: ToolAuthConfig, **kwargs) -> ToolAuthResult:
        try:
            url = auth_config.config.get("url", "")
            # Default to True if url is not provided to maintain backward compatibility
            url_is_https = url.lower().startswith("https://") if url else True
            ssl_verify, ssl_cert = SslUtils.get_ssl_config(
                auth_config.config.get("verify_switch_env", "SSL_VERIFY"),
                auth_config.config.get("ssl_cert_env", "SSL_CERT"),
                ["false"],
                url_is_https=url_is_https
            )
            if ssl_verify:
                ssl_context = SslUtils.create_strict_ssl_context(ssl_cert)
                connector = aiohttp.TCPConnector(ssl=ssl_context)
            else:
                connector = aiohttp.TCPConnector(ssl=False)
        except Exception as e:
            logger.error(f"Failed to create SSL connector: {e}")
            raise Exception(f"Failed to create SSL connector: {e}") from e
        return ToolAuthResult(
            success=True,
            auth_data={"connector": connector},
            message="SSL authentication configured"
        )


class HeaderQueryAuthStrategy(AuthStrategy):
    """Header and query parameter authentication strategy"""
    auth_type = AuthType.HEADER_AND_QUERY

    async def authenticate(self, auth_config: ToolAuthConfig, **kwargs) -> ToolAuthResult:
        if auth_config.config.get("auth_headers") is not None \
                or auth_config.config.get("auth_query_params") is not None:
            auth_provider = AuthHeaderAndQueryProvider(
                auth_headers=auth_config.config.get("auth_headers") or {},
                auth_query_params=auth_config.config.get("auth_query_params") or {},
            )
            logger.info(f"Using custom header and query authorization for {auth_config.tool_type}")
        else:
            auth_provider = None
        return ToolAuthResult(
            success=True,
            auth_data={"auth_provider": auth_provider},
            message="Custom header and query authentication configured"
        )


class AuthStrategyRegistry:
    """Authentication strategy registry"""
    _strategies: Dict[AuthType, Type[AuthStrategy]] = {}

    @classmethod
    def register(cls, strategy_class: Type[AuthStrategy]):
        """Register an authentication strategy"""
        cls._strategies[strategy_class.auth_type] = strategy_class

    @classmethod
    async def execute_auth(cls, auth_config: ToolAuthConfig, **kwargs) -> Optional[ToolAuthResult]:
        """Execute authentication logic"""
        strategy_class = cls._strategies.get(auth_config.auth_type)
        if not strategy_class:
            logger.warning(f"Unsupported auth type: {auth_config.auth_type}")
            return ToolAuthResult(
                success=False,
                auth_data={},
                message=f"Unsupported auth type: {auth_config.auth_type}"
            )
        strategy_instance = strategy_class()
        return await strategy_instance.authenticate(auth_config, **kwargs)


# Register all authentication strategies
AuthStrategyRegistry.register(SSLAuthStrategy)
AuthStrategyRegistry.register(HeaderQueryAuthStrategy)


class AuthHeaderAndQueryProvider(httpx.Auth):
    def __init__(self, auth_headers: Dict[str, str], auth_query_params: Dict[str, str]):
        self.headers = auth_headers
        self.query_params = auth_query_params

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # Add custom headers
        if self.headers:
            for key, value in self.headers.items():
                request.headers[key] = value

        # Add custom query parameters
        if self.query_params:
            url = request.url.copy_merge_params(self.query_params)
            request.url = url

        yield request
