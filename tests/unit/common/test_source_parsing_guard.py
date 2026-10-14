"""``ast.parse`` 并发守卫的回归（CPython gh-106905）。

被测对象是 ``tests/conftest.py`` 安装的解析守卫：它把进程内的 ``ast.parse``
换成「加锁 + 重试」版本，避免多线程同时解析源码时递归深度计数互相污染，抛出
与解析内容无关的 ``SystemError: AST constructor recursion depth mismatch``。

CI 上 ``test_legacy_routes_are_fully_classified`` 曾因此误报红（同一提交在四处
环境本地全绿），本文件把「守卫会被安装」「竞态会被重试吃掉」「真实错误不会被
吞掉」三件事钉住。
"""

from __future__ import annotations

import ast
import threading

import pytest

from tests.conftest import _AST_PARSE_ATTEMPTS, _AST_RACE_MESSAGE, _guard_ast_parse

pytestmark = pytest.mark.unit


def _mismatch_error() -> SystemError:
    return SystemError(f"{_AST_RACE_MESSAGE} (before=123, after=37)")


def test_ast_parse_is_guarded_for_the_whole_session() -> None:
    """会话内 ``ast.parse`` 必须已被守卫包裹，否则并发解析仍会误报红。"""
    assert getattr(ast.parse, "__wrapped__", None) is not None


def test_guard_retries_transient_recursion_depth_mismatch() -> None:
    """偶发的深度计数竞态应被重试吃掉，调用方拿到的仍是正确解析结果。"""
    seen: list[str] = []

    def flaky(source: str, *args: object, **kwargs: object) -> ast.Module:
        seen.append(source)
        if len(seen) == 1:
            raise _mismatch_error()
        return ast.Module(body=[], type_ignores=[])

    tree = _guard_ast_parse(flaky)("x = 1")

    assert isinstance(tree, ast.Module)
    assert len(seen) == 2, "首次竞态失败后必须重试，而不是直接把用例判红"


def test_guard_surfaces_persistent_recursion_depth_mismatch() -> None:
    """重试用尽后必须原样上抛，不能把持续故障吞成静默通过。"""
    calls: list[str] = []

    def always_failing(source: str, *args: object, **kwargs: object) -> ast.Module:
        calls.append(source)
        raise _mismatch_error()

    with pytest.raises(SystemError, match=_AST_RACE_MESSAGE):
        _guard_ast_parse(always_failing)("x = 1")

    assert len(calls) == _AST_PARSE_ATTEMPTS


def test_guard_does_not_retry_unrelated_system_errors() -> None:
    """非本竞态的 SystemError 立即上抛，不做无谓重试。"""
    calls: list[str] = []

    def broken(source: str, *args: object, **kwargs: object) -> ast.Module:
        calls.append(source)
        raise SystemError("unrelated interpreter state problem")

    with pytest.raises(SystemError, match="unrelated interpreter state problem"):
        _guard_ast_parse(broken)("x = 1")

    assert len(calls) == 1


def test_guard_keeps_syntax_errors_intact() -> None:
    """语法错误照旧抛 SyntaxError——守卫不得改变了失败语义。"""
    with pytest.raises(SyntaxError):
        ast.parse("def broken(:\n")


def test_concurrent_parsing_is_serialized_and_survives() -> None:
    """多线程同时解析不得崩，也不得卡死（锁是重入的、只包住解析本身）。"""
    source = "x = [" + ", ".join(str(index) for index in range(500)) + "]"
    failures: list[BaseException] = []
    start = threading.Barrier(8)
    rounds = 20

    def worker() -> None:
        try:
            start.wait(timeout=10)
            for _ in range(rounds):
                assert ast.parse(source).body
        except BaseException as exc:  # 统一收集，交给下面的断言暴露
            failures.append(exc)

    threads = [threading.Thread(target=worker, name=f"parser-{i}") for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "解析守卫把线程卡住了（锁不可重入或持锁范围过大）"

    assert failures == []
