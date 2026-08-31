"""_pg 驱动适配层单测：占位符转换、标识符引用与桥接事件循环线程。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from jiuwen_memory.storage._pg import _convert_placeholders, _LoopRunner, _quote_ident

pytestmark = pytest.mark.unit


class TestConvertPlaceholders:
    @staticmethod
    def test_single() -> None:
        assert _convert_placeholders("a = %s") == "a = $1"

    @staticmethod
    def test_multiple_in_order() -> None:
        assert _convert_placeholders("%s %s::vector %s") == "$1 $2::vector $3"

    @staticmethod
    def test_none_untouched() -> None:
        assert _convert_placeholders("SELECT 1") == "SELECT 1", "无占位符原样返回"

    @staticmethod
    def test_cast_kept() -> None:
        assert _convert_placeholders("id = ANY(%s::text[])") == "id = ANY($1::text[])"


class TestQuoteIdent:
    @staticmethod
    def test_plain() -> None:
        assert _quote_ident("public") == '"public"'

    @staticmethod
    def test_table() -> None:
        assert _quote_ident("agent_memory_kv") == '"agent_memory_kv"'

    @staticmethod
    def test_embedded_quote_doubled() -> None:
        assert _quote_ident('we"ird') == '"we""ird"'


class TestLoopRunner:
    @staticmethod
    def test_run_executes_on_dedicated_loop_thread() -> None:
        runner = _LoopRunner()

        async def go() -> int:
            await asyncio.sleep(0)
            return threading.get_ident()

        first = runner.run(go())
        assert first != threading.get_ident(), "协程必须跑在专职线程上"

    @staticmethod
    def test_run_reuses_same_loop_thread() -> None:
        runner = _LoopRunner()

        async def go() -> int:
            return threading.get_ident()

        assert runner.run(go()) == runner.run(go()), "两次提交复用同一 loop 线程"

    @staticmethod
    def test_run_propagates_exception() -> None:
        runner = _LoopRunner()

        async def boom() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            runner.run(boom())
