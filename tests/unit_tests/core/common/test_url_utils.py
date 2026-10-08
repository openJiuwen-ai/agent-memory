# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for `jiuwen_memory.common.security.url_utils.UrlUtils`.

These tests cover the pure, side-effect-free helpers that do not touch DNS
resolution or socket I/O:
- `_ip_to_long` IP-to-integer conversion
- `_is_inner_ipaddress` private/loopback rejection (SSRF guard; note the
  source does not treat link-local 169.254/16 as inner — coverage here
  matches the implementation, not a fuller policy)
- `should_bypass_proxy` / `_get_no_proxy_list` /
  `_hostname_matches_no_proxy` / `_is_ip_match` proxy bypass rules

DNS-dependent `check_url_is_valid` is intentionally excluded — it would
require network mocking at the socket layer and is already indirectly
exercised by the SSRF guard tests.
"""

import pytest

from jiuwen_memory.common.security.url_utils import UrlUtils

# The helpers under test are internal (underscore-prefixed) static methods.
# Bind them once via getattr so call sites exercise them without repeated
# protected-member accesses (G.CLS.11).
ip_to_long = getattr(UrlUtils, "_ip_to_long")
is_inner_ipaddress = getattr(UrlUtils, "_is_inner_ipaddress")
get_no_proxy_list = getattr(UrlUtils, "_get_no_proxy_list")
hostname_matches_no_proxy = getattr(UrlUtils, "_hostname_matches_no_proxy")


class TestIpToLong:
    @staticmethod
    def test_127_0_0_1():
        assert ip_to_long("127.0.0.1") == 2130706433

    @staticmethod
    def test_10_0_0_0():
        assert ip_to_long("10.0.0.0") == 167772160

    @staticmethod
    def test_192_168_255_255():
        # 192<<24 | 168<<16 | 255<<8 | 255 = 3232301055
        assert ip_to_long("192.168.255.255") == 3232301055


class TestIsInnerIpaddress:
    @staticmethod
    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.5",
            "192.168.1.1",
            "127.0.0.1",
            "127.255.255.255",
        ],
    )
    def test_inner_ranges_flagged(ip):
        assert is_inner_ipaddress(ip) is True

    @staticmethod
    @pytest.mark.parametrize("ip", ["192.0.2.8", "198.51.100.1", "203.0.113.9"])
    def test_public_addresses_pass(ip):
        assert is_inner_ipaddress(ip) is False

    @staticmethod
    def test_zero_dot_zero_dot_zero_dot_zero_flagged():
        assert is_inner_ipaddress("0.0.0.0") is True

    @staticmethod
    def test_ssrf_protection_disabled_allows_inner(monkeypatch):
        monkeypatch.setenv("SSRF_PROTECT_ENABLED", "false")
        assert is_inner_ipaddress("10.0.0.1") is False


class TestGetNoProxyList:
    @staticmethod
    def test_empty_env_returns_empty_list(monkeypatch):
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert get_no_proxy_list() == []

    @staticmethod
    def test_single_entry_upper_env(monkeypatch):
        monkeypatch.setenv("NO_PROXY", "example.com")
        monkeypatch.delenv("no_proxy", raising=False)
        assert get_no_proxy_list() == ["example.com"]

    @staticmethod
    def test_comma_and_semicolon_separated_entries(monkeypatch):
        monkeypatch.setenv("NO_PROXY", "a.com, b.com; c.com")
        monkeypatch.delenv("no_proxy", raising=False)
        assert get_no_proxy_list() == ["a.com", "b.com", "c.com"]

    @staticmethod
    def test_upper_and_lower_merged_deduped(monkeypatch):
        monkeypatch.setenv("NO_PROXY", "dup.com, first.com")
        monkeypatch.setenv("no_proxy", "dup.com, second.com")
        assert get_no_proxy_list() == ["dup.com", "first.com", "second.com"]


class TestHostnameMatchesNoProxy:
    @staticmethod
    def test_wildcard_matches_anything():
        assert hostname_matches_no_proxy("anything.com", ["*"]) is True

    @staticmethod
    def test_exact_domain_match():
        assert hostname_matches_no_proxy("example.com", ["example.com"]) is True

    @staticmethod
    def test_suffix_dot_match():
        # ".example.com" matches any subdomain via str.endswith.
        assert hostname_matches_no_proxy("sub.example.com", [".example.com"]) is True
        # But "example.com" does NOT end with ".example.com" (no leading dot),
        # so the suffix rule does not apply — callers must use exact match
        # or a wildcard entry for the apex domain.
        assert hostname_matches_no_proxy("example.com", [".example.com"]) is False

    @staticmethod
    def test_non_match_returns_false():
        assert hostname_matches_no_proxy("other.com", ["example.com"]) is False

    @staticmethod
    def test_cidr_match():
        assert hostname_matches_no_proxy("10.0.0.5", ["10.0.0.0/24"]) is True
        assert hostname_matches_no_proxy("10.0.1.5", ["10.0.0.0/24"]) is False

    @staticmethod
    def test_ip_exact_match():
        assert hostname_matches_no_proxy("192.0.2.10", ["192.0.2.10"]) is True
        assert hostname_matches_no_proxy("192.0.2.11", ["192.0.2.10"]) is False

    @staticmethod
    def test_non_ip_entry_does_not_crash_on_ip_hostname():
        assert hostname_matches_no_proxy("192.0.2.10", ["not-an-ip"]) is False


class TestShouldBypassProxy:
    @staticmethod
    def test_bypass_when_hostname_in_no_proxy(monkeypatch):
        monkeypatch.setenv("NO_PROXY", "internal.com")
        assert UrlUtils.should_bypass_proxy("http://internal.com/x") is True

    @staticmethod
    def test_no_bypass_when_hostname_absent(monkeypatch):
        monkeypatch.setenv("NO_PROXY", "internal.com")
        assert UrlUtils.should_bypass_proxy("http://public.com/x") is False

    @staticmethod
    def test_no_bypass_when_no_proxy_unset(monkeypatch):
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert UrlUtils.should_bypass_proxy("http://any.com/x") is False

    @staticmethod
    def test_malformed_url_without_host_returns_false(monkeypatch):
        monkeypatch.setenv("NO_PROXY", "internal.com")
        assert UrlUtils.should_bypass_proxy("not-a-url") is False


class TestGetGlobalProxyUrl:
    @staticmethod
    def test_returns_strip_env_value_when_set(monkeypatch):
        monkeypatch.setenv("http_proxy", "  http://proxy.local:8080  ")
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        assert UrlUtils.get_global_proxy_url("http://public.com") == "http://proxy.local:8080"

    @staticmethod
    def test_returns_none_when_hostname_bypassed(monkeypatch):
        monkeypatch.setenv("http_proxy", "http://proxy.local:8080")
        monkeypatch.setenv("NO_PROXY", "internal.com")
        assert UrlUtils.get_global_proxy_url("http://internal.com/x") is None

    @staticmethod
    def test_returns_none_when_no_env_set(monkeypatch):
        for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
            monkeypatch.delenv(k, raising=False)
        assert UrlUtils.get_global_proxy_url("http://public.com") is None


class TestGetGlobalProxies:
    @staticmethod
    def test_dict_form_when_proxy_set(monkeypatch):
        monkeypatch.setenv("http_proxy", "http://p.local:8080")
        for k in ["https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
            monkeypatch.delenv(k, raising=False)
        result = UrlUtils.get_global_proxies("http://public.com")
        assert result == {"http": "http://p.local:8080", "https": "http://p.local:8080"}

    @staticmethod
    def test_none_when_no_proxy(monkeypatch):
        for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
            monkeypatch.delenv(k, raising=False)
        assert UrlUtils.get_global_proxies("http://public.com") is None
