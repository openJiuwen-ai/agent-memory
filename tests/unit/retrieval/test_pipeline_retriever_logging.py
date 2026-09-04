"""PipelineRetriever logging helpers."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import (
    _format_channels,
    _safe_error,
    _scope_log_dims,
)
from jiuwen_memory.retrieval.types import RecallChannel

pytestmark = pytest.mark.unit


def test_safe_error_redacts_common_secret_shapes() -> None:
    exc = RuntimeError(
        "failed redis://user:pass@example.test:6379/0\n"
        "token=abc123; password: hunter2, api_key=xyz"
    )

    text = _safe_error(exc)

    assert "pass@example" not in text
    assert "abc123" not in text
    assert "hunter2" not in text
    assert "xyz" not in text
    assert "\n" not in text
    assert "token=<redacted>" in text


def test_safe_error_redacts_url_password_with_empty_username() -> None:
    text = _safe_error(RuntimeError("failed redis://:pass@example.test:6379/0"))

    assert "pass@example" not in text
    assert "//<redacted>:<redacted>@" in text


def test_safe_error_redacts_authorization_header_values() -> None:
    text = _safe_error(
        RuntimeError(
            "request failed Authorization: Bearer ey.secret.jwt "
            "headers={'Authorization': 'Basic dXNlcjpwYXNz'}"
        )
    )

    assert "ey.secret.jwt" not in text
    assert "dXNlcjpwYXNz" not in text
    assert "Authorization" in text
    assert "Authorization: Bearer <redacted>" in text
    assert "'Authorization': 'Basic <redacted>'" in text


def test_safe_error_redacts_authorization_values_without_known_scheme() -> None:
    text = _safe_error(RuntimeError("request failed Authorization: custom-token"))

    assert "custom-token" not in text
    assert "Authorization: <redacted>" in text


def test_format_channels_handles_auto_all_and_explicit_channels() -> None:
    assert _format_channels(None, auto_label="auto") == "auto"
    assert _format_channels([], auto_label="auto") == "all"
    assert (
        _format_channels([RecallChannel.KEYWORD, RecallChannel.VECTOR], auto_label="auto")
        == "keyword,vector"
    )


def test_scope_log_dims_reports_only_present_dimensions() -> None:
    assert _scope_log_dims(Scope()) == "none"
    assert _scope_log_dims(Scope(org="o", space="p", user="u", session="s")) == (
        "org,space,user,session"
    )
