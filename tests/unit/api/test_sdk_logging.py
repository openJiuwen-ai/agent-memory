"""SDK 装配日志接管行为。"""

from __future__ import annotations

import io
import logging
import sys
import threading
from collections import ChainMap, UserDict, UserList, deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jiuwen_memory.api import assemble, assemble_runtime
from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import base as log_base
from jiuwen_memory.common.log import get_logger, redact_for_log
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.config import Config


class _RecordHandler(logging.Handler):
    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _BlockingHandler(_RecordHandler):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.entered = entered
        self.resume_event = release

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.entered.set()
        if not self.resume_event.wait(5):
            raise TimeoutError("test handler was not released")


class _MutatingHandler(_RecordHandler):
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if record.name != "agent_memory.tests.copy_isolation":
            return
        record.msg = "mutated-message"
        record.args = ("mutated-args",)
        record.exc_text = "mutated-exception"
        record.payload["nested"].append("mutated-extra")


class _OpaqueString:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class _OpaqueMutatingHandler(_RecordHandler):
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        record.args[0].value = "changed"


class _ContainerMutatingHandler(_RecordHandler):
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        payload = record.payload
        payload["user_dict"]["items"].append("changed-user-dict")
        payload["user_list"].append("changed-user-list")
        payload["deque"].append("changed-deque")
        payload["chain"]["nested"].append("changed-chain-map")
        payload["dict_sub"]["items"].append("changed-dict-subclass")
        payload["list_sub"][0]["nested"].append("changed-list-subclass")


class _FailingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        del record
        raise RuntimeError("private-handler-exception-text")


_PRIVATE_BAD_LOG_BODY = "private-bad-container-body"


class _BadDict(dict):
    def items(self):
        raise RuntimeError("private-bad-dict-exception")


class _BadList(list):
    def __iter__(self):
        raise RuntimeError("private-bad-list-exception")


class _BadTuple(tuple):
    def __iter__(self):
        raise RuntimeError("private-bad-tuple-exception")


class _InterruptingList(list):
    def __iter__(self):
        raise KeyboardInterrupt


def _bad_log_container(container_kind: str) -> object:
    if container_kind == "dict":
        return _BadDict(secret=_PRIVATE_BAD_LOG_BODY)
    if container_kind == "list":
        return _BadList([_PRIVATE_BAD_LOG_BODY])
    return _BadTuple((_PRIVATE_BAD_LOG_BODY,))


class _OneShotBrokenMapping(Mapping):
    def __init__(self) -> None:
        self._values = {"nested": ["mapping-value"]}
        self.calls = 0

    def __getitem__(self, key: str):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def items(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("private-mapping-error")
        return self._values.items()


class _RecursiveHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def emit(self, record: logging.LogRecord) -> None:
        del record
        self.calls += 1
        get_logger("tests.recursive_callback").warning("nested callback log")


class _CloseCountingHandler(_RecordHandler):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


@pytest.mark.unit
@pytest.mark.parametrize("with_handler", [False, True])
def test_unspecified_output_defaults_to_builtin_even_with_handler(
    with_handler: bool, tmp_path: Path, capsys
) -> None:
    handler = _RecordHandler() if with_handler else None
    log_file = tmp_path / "default.log"
    runtime = assemble_runtime(
        config={"globals": {"log_file": str(log_file)}}, log_handler=handler
    )
    try:
        get_logger("tests.default_output").warning("default-builtin-output")
        assert "default-builtin-output" in capsys.readouterr().err
        assert "default-builtin-output" in log_file.read_text(encoding="utf-8")
        if handler is not None:
            assert not handler.records, "未选择业务输出时，不应调用传入的处理器"
            assert handler.formatter is None, "未启用的业务处理器不应被设置格式"
    finally:
        runtime.close()


@pytest.mark.unit
def test_real_sdk_assembly_captures_existing_logger_name_and_call_location() -> None:
    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={"globals": {"log_output": "business"}, "permission": {"default": "sqlite"}},
        log_handler=handler,
    )
    try:
        record = next(
            record
            for record in handler.records
            if "不再处于内容读写路径" in record.getMessage()
        )
        assert record.name == "agent_memory.api.memory_api_impl.assembly"
        assert record.pathname.endswith("assembly.py")
        assert record.funcName == "_warn_inert_permission_namespace"
    finally:
        runtime.close()


@pytest.mark.unit
def test_sdk_routes_agent_memory_and_jiuwen_memory_without_renaming() -> None:
    handler = _RecordHandler()
    runtime = assemble_runtime(config={"globals": {"log_output": "business"}}, log_handler=handler)
    try:
        assert handler.formatter is not None
        probe = logging.LogRecord("format-probe", logging.INFO, __file__, 1, "body", (), None)
        expected_time = handler.formatter.formatTime(probe, "%Y-%m-%d %H:%M:%S")
        assert handler.formatter.format(probe) == f"[{expected_time}] format-probe INFO body"
        handler.records.clear()
        get_logger("tests.agent_namespace").warning("from agent namespace")
        logging.getLogger("jiuwen_memory.tests.direct_namespace").warning(
            "from jiuwen namespace"
        )

        assert [record.name for record in handler.records] == [
            "agent_memory.tests.agent_namespace",
            "jiuwen_memory.tests.direct_namespace",
        ]
        assert [record.getMessage() for record in handler.records] == [
            "from agent namespace",
            "from jiuwen namespace",
        ]
    finally:
        runtime.close()


@pytest.mark.unit
@pytest.mark.parametrize("output", ["business", "both"])
@pytest.mark.parametrize("namespace", ["agent_memory", "jiuwen_memory"])
@pytest.mark.parametrize("slot", ["msg", "args"])
@pytest.mark.parametrize("container_kind", ["dict", "list", "tuple"])
def test_privacy_filter_fails_closed_on_container_errors_and_recovers(
    output: str,
    namespace: str,
    slot: str,
    container_kind: str,
    tmp_path: Path,
    capsys,
) -> None:
    log_file = tmp_path / f"privacy-{output}-{namespace}-{slot}-{container_kind}.log"
    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": output,
                "log_level": "INFO",
                "log_format": "%(message)s",
                "log_file": str(log_file),
            }
        },
        log_handler=handler,
    )
    logger_name = f"review.privacy.{output}.{slot}.{container_kind}"
    logger = (
        get_logger(logger_name)
        if namespace == "agent_memory"
        else logging.getLogger(f"jiuwen_memory.{logger_name}")
    )
    try:
        capsys.readouterr()
        handler.records.clear()
        if output == "both":
            log_file.write_text("", encoding="utf-8")
        payload = _bad_log_container(container_kind)
        if slot == "msg":
            logger.info(payload)
        else:
            logger.info("private payload %s", payload)

        failure = capsys.readouterr()
        file_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        assert not handler.records, "脱敏失败的记录不得交付业务 Handler"
        assert file_text == "", "脱敏失败的记录不得写入 SDK 文件"
        assert "privacy filtering" in failure.err
        assert "RuntimeError" in failure.err
        assert "test_sdk_logging.py:" in failure.err and " in " in failure.err
        assert _PRIVATE_BAD_LOG_BODY not in failure.err + failure.out + file_text
        assert "private-bad-" not in failure.err + failure.out + file_text

        logger.info("safe-follow-up")
        assert [record.getMessage() for record in handler.records] == ["safe-follow-up"]
        if output == "both":
            assert "safe-follow-up" in log_file.read_text(encoding="utf-8")
        else:
            assert not log_file.exists()
    finally:
        runtime.close()


@pytest.mark.unit
@pytest.mark.parametrize("namespace", ["agent_memory", "jiuwen_memory"])
def test_privacy_failure_diagnostic_stderr_failure_is_swallowed(
    namespace: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenStderr:
        @staticmethod
        def write(value: str) -> int:
            del value
            raise OSError("private-diagnostic-stderr")

    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={"globals": {"log_output": "business"}}, log_handler=handler
    )
    logger_name = f"review.privacy.broken_stderr.{namespace}"
    logger = (
        get_logger(logger_name)
        if namespace == "agent_memory"
        else logging.getLogger(f"jiuwen_memory.{logger_name}")
    )
    monkeypatch.setattr("sys.stderr", _BrokenStderr())
    try:
        handler.records.clear()
        logger.info("private payload %s", _BadList([_PRIVATE_BAD_LOG_BODY]))
        logger.info("safe-follow-up")

        assert [record.getMessage() for record in handler.records] == ["safe-follow-up"]
    finally:
        runtime.close()


@pytest.mark.unit
@pytest.mark.parametrize("namespace", ["agent_memory", "jiuwen_memory"])
def test_privacy_diagnostic_reentry_is_suppressed(namespace: str) -> None:
    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={"globals": {"log_output": "business"}}, log_handler=handler
    )
    logger_name = f"review.privacy.reentry.{namespace}"
    logger = (
        get_logger(logger_name)
        if namespace == "agent_memory"
        else logging.getLogger(f"jiuwen_memory.{logger_name}")
    )

    class _ReentrantStderr:
        def __init__(self) -> None:
            self.calls = 0

        def write(self, value: str) -> int:
            self.calls += 1
            if self.calls == 1:
                logger.info("private payload %s", _BadList([_PRIVATE_BAD_LOG_BODY]))
            return len(value)

    diagnostic_stderr = _ReentrantStderr()
    original_stderr = sys.stderr
    sys.stderr = diagnostic_stderr
    try:
        handler.records.clear()
        logger.info("private payload %s", _BadList([_PRIVATE_BAD_LOG_BODY]))

        assert diagnostic_stderr.calls == 1, "stderr 写入引起的日志不得递归诊断"
        assert not handler.records
        logger.info("safe-follow-up")
        assert [record.getMessage() for record in handler.records] == ["safe-follow-up"]
    finally:
        sys.stderr = original_stderr
        runtime.close()


@pytest.mark.unit
@pytest.mark.parametrize("namespace", ["agent_memory", "jiuwen_memory"])
def test_privacy_filter_does_not_swallow_baseexception(namespace: str) -> None:
    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={"globals": {"log_output": "business"}}, log_handler=handler
    )
    logger_name = f"review.privacy.baseexception.{namespace}"
    logger = (
        get_logger(logger_name)
        if namespace == "agent_memory"
        else logging.getLogger(f"jiuwen_memory.{logger_name}")
    )
    try:
        handler.records.clear()
        with pytest.raises(KeyboardInterrupt):
            logger.info("payload %s", _InterruptingList([_PRIVATE_BAD_LOG_BODY]))

        logger.info("safe-follow-up")
        assert [record.getMessage() for record in handler.records] == ["safe-follow-up"]
    finally:
        runtime.close()


@pytest.mark.unit
@pytest.mark.parametrize("output", ["builtin", "business", "both"])
def test_builtin_business_and_both_outputs(output: str, tmp_path: Path, capsys) -> None:
    log_file = tmp_path / "sdk.log"
    business_stream = io.StringIO()
    handler = (
        logging.StreamHandler(business_stream) if output in {"business", "both"} else None
    )
    prefix = output.upper()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": output,
                "log_file": str(log_file),
                "log_format": f"{prefix} %(message)s",
            }
        },
        log_handler=handler,
    )
    try:
        get_logger(f"tests.{output}").warning(f"{output}-output")
        if output in {"business", "both"}:
            assert f"{prefix} {output}-output" in business_stream.getvalue()
        if output in {"builtin", "both"}:
            assert f"{prefix} {output}-output" in log_file.read_text(encoding="utf-8")
        if output == "builtin":
            assert f"{prefix} {output}-output" in capsys.readouterr().err
        elif output == "business":
            assert not log_file.exists(), "business 模式不应创建 SDK 内置文件"
    finally:
        runtime.close()


@pytest.mark.unit
def test_business_formatter_level_and_filter_are_respected() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("CUSTOM %(levelname)s %(message)s"))
    handler.setLevel(logging.ERROR)
    handler.addFilter(lambda record: "accepted" in record.getMessage())
    runtime = assemble_runtime(config={"globals": {"log_output": "business"}}, log_handler=handler)
    try:
        logger = get_logger("tests.handler_options")
        logger.warning("accepted warning")
        logger.error("filtered error")
        logger.error("accepted error")
        assert stream.getvalue() == "CUSTOM ERROR accepted error\n"
    finally:
        runtime.close()


@pytest.mark.unit
def test_yaml_log_filters_are_independent_and_match_redacted_interpolated_message(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "filtered-sdk.log"
    config_file = tmp_path / "sdk-logging.yml"
    config_file.write_text(
        f"""globals:
  log_output: both
  log_level: INFO
  log_format: "SDK %(message)s"
  log_file: {log_file.as_posix()}
  log_filters:
    business:
      include: '^query.*retry 7 secret=\\*'
      exclude: 'blocked'
    builtin:
      include: '^admin.*retry 7 secret=\\*'
      exclude: 'blocked'
""",
        encoding="utf-8",
    )
    business_stream = io.StringIO()
    handler = logging.StreamHandler(business_stream)
    handler.setFormatter(logging.Formatter("BUSINESS %(message)s"))
    seen_records: list[logging.LogRecord] = []
    handler.addFilter(lambda record: seen_records.append(record) or True)

    runtime = assemble_runtime(config=Config.from_yaml(str(config_file)), log_handler=handler)
    try:
        log_file.write_text("", encoding="utf-8")
        seen_records.clear()
        logger = get_logger("tests.yaml_log_filters")
        logger.info("query retry %d secret=%s", 7, redact_for_log("private-token"))
        logger.info("admin retry %d secret=%s", 7, redact_for_log("private-token"))
        logger.info("query retry %d secret=%s blocked", 7, redact_for_log("private-token"))
        logger.info("admin retry %d secret=%s blocked", 7, redact_for_log("private-token"))
        handler.flush()

        assert business_stream.getvalue() == "BUSINESS query retry 7 secret=*\n"
        assert len(seen_records) == 1
        assert seen_records[0].msg == "query retry %d secret=%s"
        assert seen_records[0].args == (7, "*"), "Handler 收到的 msg/args 仍保持参数化结构"
        builtin_text = log_file.read_text(encoding="utf-8")
        assert builtin_text == "SDK admin retry 7 secret=*\n"
        assert "private-token" not in business_stream.getvalue() + builtin_text
        assert "blocked" not in business_stream.getvalue() + builtin_text
    finally:
        runtime.close()


@pytest.mark.unit
def test_all_filtered_routes_prepare_match_before_business_handler_mutates_opaque_arg(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "opaque-filter.log"
    business_handler = _OpaqueMutatingHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_file": str(log_file),
                "log_format": "%(message)s",
                "log_filters": {
                    "business": {"include": "opaque=original"},
                    "builtin": {"include": "opaque=original"},
                },
            }
        },
        log_handler=business_handler,
    )
    try:
        log_file.write_text("", encoding="utf-8")
        business_handler.records.clear()
        get_logger("tests.opaque_filter_snapshot").warning("opaque=%s", _OpaqueString("original"))

        assert len(business_handler.records) == 1
        # 自定义对象仍可能跨副本共享；但其修改发生在两路已完成匹配之后。
        assert log_file.read_text(encoding="utf-8") == "opaque=changed\n"
    finally:
        runtime.close()


@pytest.mark.unit
def test_unfiltered_route_does_not_prepare_message_that_raises() -> None:
    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={"globals": {"log_output": "business"}}, log_handler=handler
    )
    try:
        handler.records.clear()
        get_logger("tests.unformatted_message").warning("number=%d", "not-an-integer")

        assert len(handler.records) == 1
        assert handler.records[0].msg == "number=%d"
        assert handler.records[0].args == ("not-an-integer",)
    finally:
        runtime.close()


@pytest.mark.unit
def test_message_preparation_failure_is_isolated_to_filtered_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = io.StringIO()
    monkeypatch.setattr("sys.stderr", diagnostics)
    business_handler = _RecordHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_filters": {"builtin": {"include": "must-match"}},
            }
        },
        log_handler=business_handler,
    )
    try:
        business_handler.records.clear()
        get_logger("tests.filter_message_error").warning("number=%d", "not-an-integer")

        assert len(business_handler.records) == 1
        assert business_handler.records[0].msg == "number=%d"
        assert business_handler.records[0].args == ("not-an-integer",)
        assert "log filter match" in diagnostics.getvalue()
        assert "TypeError" in diagnostics.getvalue()
        assert "not-an-integer" not in diagnostics.getvalue()
    finally:
        runtime.close()


@pytest.mark.unit
@pytest.mark.parametrize("output", ["business", "both"])
@pytest.mark.parametrize("namespace", ["agent_memory", "jiuwen_memory"])
@pytest.mark.parametrize("business_level", [logging.NOTSET, logging.CRITICAL])
def test_sdk_level_cannot_be_bypassed_by_child_logger(
    output: str, namespace: str, business_level: int, tmp_path: Path
) -> None:
    handler = _RecordHandler(level=business_level)
    handler.addFilter(lambda record: record.getMessage() != "excluded-by-business")
    log_file = tmp_path / "sdk.log"
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": output,
                "log_level": "ERROR",
                "log_file": str(log_file),
            }
        },
        log_handler=handler,
    )
    name = (
        "agent_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer"
        if namespace == "agent_memory" else "jiuwen_memory.common._import_support"
    )
    logger = logging.getLogger(name)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        # 真实组件调用产生低于 SDK ERROR 门槛的 INFO / WARNING 记录。
        if namespace == "agent_memory":
            assert WhitespaceTokenizer().tokenize("hello world") == ["hello", "world"]
        else:
            assert import_optional("sdk_logging_missing_module_for_regression") is None
        assert not handler.records, "子日志器不能绕过 SDK 的统一日志级别"
        if output == "both":
            assert log_file.read_text(encoding="utf-8") == ""

        logger.error("sdk-error")
        logger.critical("sdk-critical")
        logger.critical("excluded-by-business")
        expected = ["sdk-critical"]
        if business_level == logging.NOTSET:
            expected.insert(0, "sdk-error")
        assert [record.getMessage() for record in handler.records] == expected
        assert handler.level == business_level, "不能覆盖业务处理器自己的级别"
        if output == "both":
            text = log_file.read_text(encoding="utf-8")
            assert "sdk-error" in text and "sdk-critical" in text
            assert "excluded-by-business" in text, "业务过滤条件不能影响内置输出"
        else:
            assert not log_file.exists()
    finally:
        logger.setLevel(previous_level)
        runtime.close()


@pytest.mark.unit
def test_both_outputs_receive_independent_sanitized_record_copies(tmp_path: Path) -> None:
    log_file = tmp_path / "isolated.log"
    handler = _MutatingHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_file": str(log_file),
                "log_format": "%(message)s",
            }
        },
        log_handler=handler,
    )
    try:
        handler.records.clear()
        log_file.write_text("", encoding="utf-8")
        get_logger("tests.copy_isolation").warning(
            "private=%s",
            redact_for_log("private-body"),
            extra={"payload": {"nested": ["original-extra"]}},
        )
        assert handler.records[0].payload["nested"] == [
            "original-extra",
            "mutated-extra",
        ]
        builtin_text = log_file.read_text(encoding="utf-8")
        assert builtin_text == "private=*\n"
        assert "mutated" not in builtin_text
        assert "private-body" not in builtin_text
    finally:
        runtime.close()


@pytest.mark.unit
def test_standard_mutable_container_copies_protect_builtin_and_callers(
    tmp_path: Path,
) -> None:
    class _DictSubclass(dict):
        pass

    class _ListSubclass(list):
        pass

    payload = _DictSubclass(
        {
            "user_dict": UserDict({"items": UserList(["original-user-dict"])}),
            "user_list": UserList(["original-user-list"]),
            "deque": deque(["original-deque"]),
            "chain": ChainMap({"nested": ["original-chain-map"]}),
            "dict_sub": _DictSubclass({"items": ["original-dict-subclass"]}),
            "list_sub": _ListSubclass([{"nested": ["original-list-subclass"]}]),
        }
    )
    original_payload = repr(payload)
    log_file = tmp_path / "container-copies.log"
    handler = _ContainerMutatingHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_file": str(log_file),
                "log_format": "%(payload)s",
            }
        },
        log_handler=handler,
    )
    try:
        log_file.write_text("", encoding="utf-8")
        handler.records.clear()
        get_logger("tests.container_copies").warning(
            "container payload", extra={"payload": payload}
        )

        builtin_text = log_file.read_text(encoding="utf-8")
        assert "changed-" not in builtin_text
        assert "original-user-dict" in builtin_text
        assert "original-user-list" in builtin_text
        assert "original-deque" in builtin_text
        assert "original-chain-map" in builtin_text
        assert "original-dict-subclass" in builtin_text
        assert "original-list-subclass" in builtin_text
        assert repr(payload) == original_payload
        assert "changed-user-dict" in repr(handler.records[0].payload)
    finally:
        runtime.close()


@pytest.mark.unit
def test_exception_record_keeps_redacted_traceback_formatable(tmp_path: Path) -> None:
    log_file = tmp_path / "exception.log"
    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_file": str(log_file),
                "log_format": "%(message)s",
            }
        },
        log_handler=handler,
    )
    try:
        try:
            raise RuntimeError("private-exception-content")
        except RuntimeError:
            get_logger("tests.exception_record").exception("operation failed")

        record = handler.records[-1]
        assert record.exc_info is not None
        assert record.exc_info[1].args == ("*",)
        assert record.exc_text is not None
        assert "Traceback (most recent call last)" in record.exc_text
        assert "RuntimeError: *" in record.exc_text
        assert "private-exception-content" not in record.exc_text
        file_text = log_file.read_text(encoding="utf-8")
        assert "RuntimeError: *" in file_text
        assert "private-exception-content" not in file_text
    finally:
        runtime.close()


@pytest.mark.unit
def test_handler_failure_is_redacted_and_does_not_interrupt_builtin_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = tmp_path / "handler-failure.log"
    diagnostics = io.StringIO()
    monkeypatch.setattr("sys.stderr", diagnostics)
    handler = _FailingHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_file": str(log_file),
                "log_format": "%(message)s",
            }
        },
        log_handler=handler,
    )
    try:
        get_logger("tests.handler_failure").warning("still-running")
        diagnostic = diagnostics.getvalue()
        assert "_FailingHandler" in diagnostic
        assert "RuntimeError" in diagnostic
        assert "test_sdk_logging.py:" in diagnostic
        assert " in emit" in diagnostic
        assert "private-handler-exception-text" not in diagnostic
        assert "still-running" in log_file.read_text(encoding="utf-8")
    finally:
        runtime.close()


@pytest.mark.unit
def test_mapping_copy_failure_isolated_per_output_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = tmp_path / "mapping-error.log"
    diagnostics = io.StringIO()
    monkeypatch.setattr("sys.stderr", diagnostics)
    handler = _RecordHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_file": str(log_file),
                "log_format": "%(message)s %(payload)s",
            }
        },
        log_handler=handler,
    )
    try:
        log_file.write_text("", encoding="utf-8")
        handler.records.clear()
        mapping = _OneShotBrokenMapping()
        get_logger("tests.mapping_copy_error").warning(
            "mapping payload", extra={"payload": mapping}
        )

        assert mapping.calls == 3
        assert "mapping payload {'nested': ['mapping-value']}" in log_file.read_text(
            encoding="utf-8"
        )
        assert "_RecordHandler" in diagnostics.getvalue()
        assert "RuntimeError" in diagnostics.getvalue()
        assert "private-mapping-error" not in diagnostics.getvalue()
    finally:
        runtime.close()


@pytest.mark.unit
def test_stderr_diagnostic_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenStderr:
        @staticmethod
        def write(value: str) -> int:
            del value
            raise OSError("stderr unavailable")

    monkeypatch.setattr("sys.stderr", _BrokenStderr())
    runtime = assemble_runtime(
        config={"globals": {"log_output": "business"}}, log_handler=_FailingHandler()
    )
    try:
        get_logger("tests.broken_stderr").warning("still handled")
    finally:
        runtime.close()


@pytest.mark.unit
def test_business_handler_recursion_does_not_reenter_itself(tmp_path: Path) -> None:
    log_file = tmp_path / "recursive.log"
    handler = _RecursiveHandler()
    runtime = assemble_runtime(
        config={
            "globals": {
                "log_output": "both",
                "log_file": str(log_file),
                "log_format": "%(message)s",
            }
        },
        log_handler=handler,
    )
    try:
        handler.calls = 0
        log_file.write_text("", encoding="utf-8")
        get_logger("tests.recursive_source").warning("outer log")
        assert handler.calls == 1
        assert log_file.read_text(encoding="utf-8") == "outer log\n"
    finally:
        runtime.close()


@pytest.mark.unit
def test_log_configuration_is_process_shared_idempotent_and_conflicts_are_rejected() -> None:
    handler = _CloseCountingHandler()
    first = assemble_runtime(config={"globals": {"log_output": "business"}}, log_handler=handler)
    second = assemble_runtime(
        config={"globals": {"log_output": "business"}}, log_handler=None
    )
    third = assemble_runtime(log_handler=handler)
    first.close()
    second.close()
    third.close()
    assert handler.close_calls == 0, "runtime.close 不得关闭业务 handler"
    handler.records.clear()
    get_logger("tests.after_runtime_close").warning("still globally routed")
    assert len(handler.records) == 1
    assert handler.records[-1].getMessage() == "still globally routed"

    with pytest.raises(ValidationError, match="log_level"):
        assemble_runtime(config={"globals": {"log_level": "DEBUG"}}, log_handler=handler)
    with pytest.raises(ValidationError, match="log_handler"):
        assemble_runtime(config={}, log_handler=_RecordHandler())


@pytest.mark.unit
def test_log_filter_leaves_inherit_and_explicit_changes_or_clears_conflict() -> None:
    handler = _RecordHandler()
    first = assemble_runtime(
        config={
            "globals": {
                "log_output": "business",
                "log_filters": {"business": {"include": "keep"}},
            }
        },
        log_handler=handler,
    )
    second = assemble_runtime(
        config={
            "globals": {
                "log_filters": {"business": {"include": "keep", "exclude": None}}
            }
        },
        log_handler=handler,
    )
    third = assemble_runtime()
    first.close()
    second.close()
    third.close()

    handler.records.clear()
    logger = get_logger("tests.shared_log_filters")
    logger.warning("keep this")
    logger.warning("drop this")
    assert [record.getMessage() for record in handler.records] == ["keep this"]

    with pytest.raises(ValidationError, match="globals.log_filters.business.include"):
        assemble_runtime(
            config={"globals": {"log_filters": {"business": {"include": "k(eep)"}}}}
        )
    with pytest.raises(ValidationError, match="globals.log_filters.business.include"):
        assemble_runtime(
            config={"globals": {"log_filters": {"business": {"include": None}}}}
        )

    handler.records.clear()
    logger.warning("keep after conflict")
    assert [record.getMessage() for record in handler.records] == ["keep after conflict"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filters", "expected_path", "must_not_appear"),
    [
        ([], "globals.log_filters", None),
        ({"business": None}, "globals.log_filters.business", None),
        ({"secret_namespace": {}}, "globals.log_filters", "secret_namespace"),
        ({"business": {"secret_field": "value"}}, "globals.log_filters.business", "secret_field"),
        ({"business": {"include": ""}}, "globals.log_filters.business.include", None),
        ({"business": {"include": 7}}, "globals.log_filters.business.include", None),
        (
            {"business": {"include": "SECRET_PRIVATE_REGEX["}},
            "globals.log_filters.business.include",
            "SECRET_PRIVATE_REGEX",
        ),
        (
            {"builtin": {"exclude": "a{4294967296}"}},
            "globals.log_filters.builtin.exclude",
            "4294967296",
        ),
    ],
)
def test_invalid_log_filter_configuration_fails_without_installation_side_effects(
    filters: object,
    expected_path: str,
    must_not_appear: str | None,
    tmp_path: Path,
) -> None:
    handler = _RecordHandler()
    log_file = tmp_path / "must-not-be-created.log"
    roots = [logging.getLogger(name) for name in ("agent_memory", "jiuwen_memory")]
    before = [(root.level, root.propagate, tuple(root.handlers)) for root in roots]

    with pytest.raises(ValidationError) as error:
        log_base.setup_logging(
            {
                "log_output": "business",
                "log_file": str(log_file),
                "log_filters": filters,
            },
            log_handler=handler,
        )

    message = str(error.value)
    assert expected_path in message
    if must_not_appear is not None:
        assert must_not_appear not in message
    assert handler.formatter is None
    assert not log_file.exists()
    assert [(root.level, root.propagate, tuple(root.handlers)) for root in roots] == before
    # 不读取内部状态：用不同设置成功重试并交付日志，证明失败未锁定进程配置。
    log_base.setup_logging({"log_output": "business"}, log_handler=handler)
    get_logger("tests.invalid_rules_recovery").warning("rules recovered")
    assert handler.records[-1].getMessage() == "rules recovered"


@pytest.mark.unit
@pytest.mark.parametrize("output", ["business", "both"])
def test_business_outputs_require_handler(output: str) -> None:
    with pytest.raises(ValidationError, match="requires a log_handler"):
        assemble(config={"globals": {"log_output": output}})


@pytest.mark.unit
def test_failed_logging_initialization_leaves_no_sdk_router_or_file_handler(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    invalid_log_file = blocked_parent / "sdk.log"

    roots = [logging.getLogger(name) for name in ("agent_memory", "jiuwen_memory")]
    before = [(logger.level, logger.propagate, tuple(logger.handlers)) for logger in roots]
    with pytest.raises(OSError):
        log_base.setup_logging({"log_file": str(invalid_log_file)})

    assert [(logger.level, logger.propagate, tuple(logger.handlers)) for logger in roots] == before

    handler = _RecordHandler()
    log_base.setup_logging({"log_output": "business"}, log_handler=handler)
    get_logger("tests.after_failed_setup").warning("setup recovered")
    assert handler.records[-1].getMessage() == "setup recovered"


@pytest.mark.unit
def test_concurrent_identical_logging_initialization_installs_one_router() -> None:
    handler = _RecordHandler()
    roots = [logging.getLogger(name) for name in ("agent_memory", "jiuwen_memory")]
    before = [tuple(logger.handlers) for logger in roots]

    def initialize() -> None:
        log_base.setup_logging(
            {"log_output": "business", "log_level": "INFO"},
            log_handler=handler,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: initialize(), range(32)))

    get_logger("tests.concurrent_setup").warning("single route")
    assert [record.getMessage() for record in handler.records].count("single route") == 1
    additions = [
        [destination for destination in logger.handlers if destination not in original]
        for logger, original in zip(roots, before, strict=True)
    ]
    assert [len(destinations) for destinations in additions] == [1, 1]
    assert additions[0][0] is additions[1][0], "两个命名空间应共用同一个日志分发器"


@pytest.mark.unit
def test_sdk_logging_does_not_change_python_root_or_third_party_logs() -> None:
    root = logging.getLogger()
    root_stream = io.StringIO()
    root_handler = logging.StreamHandler(root_stream)
    previous_level = root.level
    previous_propagate = root.propagate
    root.addHandler(root_handler)
    handler = _RecordHandler()
    runtime = assemble_runtime(config={"globals": {"log_output": "business"}}, log_handler=handler)
    try:
        logging.getLogger("unrelated.third_party").warning("third-party-output")
        assert root.level == previous_level
        assert root.propagate == previous_propagate
        assert "third-party-output" in root_stream.getvalue()
        assert not any("third-party-output" in record.getMessage() for record in handler.records)
    finally:
        runtime.close()
        root.removeHandler(root_handler)


@pytest.mark.unit
@pytest.mark.parametrize("closed", [False, True])
def test_diagnostic_reports_write_status_without_leaking_exception_values(
    closed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwen_memory.common.log.privacy_filter import write_diagnostic

    stream = io.StringIO()
    if closed:
        stream.close()
    monkeypatch.setattr("sys.stderr", stream)
    result = write_diagnostic(
        "review-handler", RuntimeError("private-diagnostic-value"), operation="review operation"
    )

    assert result is not closed, "诊断写入成功返回 True，写入失败返回 False，不向外抛错"
    if not closed:
        text = stream.getvalue()
        assert "review-handler" in text and "RuntimeError" in text
        assert "private-diagnostic-value" not in text


@pytest.mark.unit
def test_formatter_restore_failure_is_reported_without_replacing_initialization_error(
    tmp_path: Path, capsys
) -> None:
    class RestoreFailingHandler(_RecordHandler):
        def setFormatter(self, formatter: logging.Formatter | None) -> None:
            if formatter is None:
                raise RuntimeError("private-formatter-restore-value")
            super().setFormatter(formatter)

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    handler = RestoreFailingHandler()
    try:
        with pytest.raises(OSError):
            log_base.setup_logging(
                {"log_output": "both", "log_file": str(blocked / "sdk.log")},
                log_handler=handler,
            )
        diagnostics = capsys.readouterr().err
        assert "formatter restore" in diagnostics
        assert "RestoreFailingHandler" in diagnostics and "RuntimeError" in diagnostics
        assert "private-formatter-restore-value" not in diagnostics

        # 原始装配失败后仍可使用不同设置创建 SDK，恢复失败不能锁定进程配置。
        recovered = _RecordHandler()
        runtime = assemble_runtime(
            config={"globals": {"log_output": "business"}}, log_handler=recovered
        )
        try:
            get_logger("tests.restore_recovery").warning("recovered after formatter failure")
            assert recovered.records[-1].getMessage() == "recovered after formatter failure"
        finally:
            runtime.close()
            recovered.close()
    finally:
        handler.close()


@pytest.mark.unit
@pytest.mark.parametrize("namespace", ["agent_memory", "jiuwen_memory"])
def test_router_installation_preserves_handlers_being_iterated(namespace: str) -> None:
    entered = threading.Event()
    release = threading.Event()
    observer = _BlockingHandler(entered, release)
    business = _RecordHandler()
    logger = logging.getLogger(namespace)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(observer)
    original_handlers = logger.handlers
    snapshot = tuple(original_handlers)
    errors: list[BaseException] = []

    def emit() -> None:
        try:
            logger.warning("record started before SDK installation")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=emit)
    worker.start()
    try:
        assert entered.wait(5), "原有 Handler 应先进入，确保与安装过程重叠"
        log_base.setup_logging({"log_output": "business"}, log_handler=business)
    finally:
        release.set()
        worker.join(5)
        logger.removeHandler(observer)
        observer.close()

    assert not worker.is_alive() and not errors, "输出线程应正常结束"
    assert len(observer.records) == 1, "安装路由不得让原有 Handler 重复处理同一条记录"
    assert tuple(original_handlers) == snapshot, "正在遍历的旧列表不得被 SDK 改写"
    assert logger.handlers is not original_handlers
    logger.warning("record after SDK installation")
    assert [record.getMessage() for record in business.records] == [
        "record after SDK installation"
    ], "安装成功后产生的记录应交给 SDK 业务输出"


@pytest.mark.unit
def test_router_installation_rollback_preserves_handlers_being_iterated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    observer = _BlockingHandler(entered, release)
    trailing = _RecordHandler()
    business = _RecordHandler()
    first = logging.getLogger("agent_memory")
    first.setLevel(logging.INFO)
    first.propagate = False
    first.addHandler(observer)
    first.addHandler(trailing)
    second = logging.getLogger("jiuwen_memory")
    original_set_level = second.setLevel
    failed = False
    errors: list[BaseException] = []

    def emit() -> None:
        try:
            first.warning("record started before installation rollback")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=emit)

    def fail_once(level: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            worker.start()
            assert entered.wait(5), "第一路已安装的日志应进入原有 Handler"
            raise RuntimeError("second logger installation failed")
        original_set_level(level)

    monkeypatch.setattr(second, "setLevel", fail_once)
    try:
        with pytest.raises(RuntimeError, match="second logger installation failed"):
            log_base.setup_logging({"log_output": "business"}, log_handler=business)
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(5)
        first.removeHandler(observer)
        first.removeHandler(trailing)
        observer.close()
        trailing.close()

    assert not worker.is_alive() and not errors, "回滚期间正在输出的线程应正常完成"
    assert len(observer.records) == 1
    assert len(trailing.records) == 1, "回滚不得让正在遍历的日志跳过后续应用 Handler"
