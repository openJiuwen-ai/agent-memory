"""检索运行态日志、脱敏与实际 start 路径。"""

from __future__ import annotations

import logging

import pytest

from jiuwen_memory.common.errors import ValidationError, safe_error_message
from jiuwen_memory.common.log import (
    SensitiveDataFilter,
    metadata_for_log,
    redact_for_log,
    scope_for_log,
)
from jiuwen_memory.common.type_def import HierarchyKind, Scope
from jiuwen_memory.retrieval.staged_retriever import (
    RetrieveState,
    _format_channels,
    _scope_log_dims,
)
from jiuwen_memory.retrieval.types import RecallChannel, RetrievalQuery
from tests.conftest import make_world

pytestmark = pytest.mark.unit


def test_safe_error_redacts_common_secret_shapes() -> None:
    exc = RuntimeError(
        "failed redis://user:pass@example.test:6379/0\n"
        "token=abc123; password: hunter2, api_key=xyz"
    )

    text = safe_error_message(exc)

    assert "pass@example" not in text
    assert "abc123" not in text
    assert "hunter2" not in text
    assert "xyz" not in text
    assert "\n" not in text
    assert "token=<redacted>" in text


def test_safe_error_redacts_url_password_with_empty_username() -> None:
    text = safe_error_message(RuntimeError("failed redis://:pass@example.test:6379/0"))

    assert "pass@example" not in text
    assert "//<redacted>:<redacted>@" in text


def test_safe_error_redacts_authorization_header_values() -> None:
    text = safe_error_message(
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
    text = safe_error_message(RuntimeError("request failed Authorization: custom-token"))

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


@pytest.fixture
def start_logs(caplog, monkeypatch):
    """直接捕获目标 logger，兼容先前装配关闭父 logger 传播的场景。"""
    target = logging.getLogger("agent_memory.retrieval.staged_retriever")
    monkeypatch.setattr(target, "propagate", False)
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=target.name):
            yield caplog
    finally:
        target.removeHandler(caplog.handler)


@pytest.mark.parametrize("channels,expected", [
    (None, "auto"),
    ([], "all"),
    ([RecallChannel.KEYWORD, RecallChannel.VECTOR], "keyword,vector"),
])
def test_start_logs_channel_selection_and_scope_dimensions_without_values(
    channels, expected, start_logs,
) -> None:
    state = RetrieveState.start(
        Scope(org="private-org", user="private-user", session="private-session"),
        RetrievalQuery(text="private-query", channels=channels, pipeline="fast"),
    )
    assert not start_logs.records, "构造状态不能在参数校验前记录 start 日志"
    state.log_start()

    messages = [record.getMessage() for record in start_logs.records
                if "Retriever.retrieve start:" in record.getMessage()]
    assert len(messages) == 1
    message = messages[0]
    assert f"channels={expected} " in message
    assert "scope_dims=org,user,session " in message
    assert "disclosure=l0 " in message
    assert "pipeline=fast" in message
    assert "private-" not in message
    assert state.run.scope_dims == "org,user,session"


@pytest.mark.parametrize("invalid", ["top_k", "max_tokens", "disclosure"])
def test_validation_failure_precedes_start_log_formatting(invalid, start_logs):
    retriever = make_world().retriever
    query = RetrievalQuery(text="private-query")
    if invalid == "disclosure":
        query.hierarchy_kind = HierarchyKind.TIME
        query.expand_depth = 1
        query.disclosure = "invalid"
    else:
        setattr(query, invalid, 0)
        # 如果日志仍在校验前执行，格式化通道会先抛 AttributeError。
        query.channels = [object()]

    with pytest.raises(ValidationError, match=invalid):
        retriever.retrieve(Scope(org="private-org"), query)
    assert not start_logs.records


# ---------------------------------------------------------------------------
# 本次新增测试：统一日志隐私 Filter（仅测试代码，不参与服务运行）
# ---------------------------------------------------------------------------


def _filtered_message(message: str, args: tuple[object, ...]) -> tuple[str, logging.LogRecord]:
    record = logging.LogRecord(
        name="agent_memory.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )
    assert SensitiveDataFilter().filter(record)
    return record.getMessage(), record


def test_privacy_filter_keeps_metadata_shape_and_only_allows_trusted_unit_id() -> None:
    message, _ = _filtered_message(
        "metadata=%s content=%s scope=%s",
        (
            metadata_for_log(
                {
                    "user_id": "user-visible-name",
                    "content": "private-memory-content",
                    "unit_id": "unit-001",
                    "nested": {"token": "secret-token"},
                },
                visible_memory_unit_ids=["unit-001"],
            ),
            redact_for_log("private-memory-content"),
            scope_for_log(Scope(org="tenant-a", user="alice", session="session-a")),
        ),
    )

    assert "unit-001" in message
    assert "user-visible-name" not in message
    assert "private-memory-content" not in message
    assert "secret-token" not in message
    assert "'user_id': '*'" in message
    assert "'content': '*'" in message
    assert "'nested': {'token': '*'}" in message
    assert "'session': '*'" in message


def test_privacy_filter_replaces_exception_message_and_keeps_traceback() -> None:
    message, record = _filtered_message(
        "operation failed: %s",
        (RuntimeError("response contains private-memory-content"),),
    )
    try:
        raise ValueError("traceback contains private-memory-content")
    except ValueError as exc:
        original_traceback = exc.__traceback__
        record.exc_info = (type(exc), exc, original_traceback)
    assert SensitiveDataFilter().filter(record)

    assert message == "operation failed: RuntimeError"
    assert record.exc_info is not None
    assert record.exc_info[0] is ValueError
    assert str(record.exc_info[1]) == "*"
    assert record.exc_info[2] is original_traceback
    assert "Traceback (most recent call last):" in record.exc_text
    assert "ValueError: *" in record.exc_text
    assert "private-memory-content" not in record.exc_text
