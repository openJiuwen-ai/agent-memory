# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""入口点矩阵：``pip install`` 后 CLI / HTTP / MCP 三个命令必须可直接调用。

本文件锁四件事：

1. ``[project.scripts]`` 声明了三个命令面，且 target 指向可解析的 ``module:attr``；
2. ``[project.scripts]`` 每个 target 所在的子包都不被 ``packages.find.exclude`` 命中
   ——``cli`` / ``mcp_server`` 曾被 exclude 导致装完只有 SDK，这是本 PR 要防的回归类别；
3. 命令名与各入口的实际程序名一致，避免 ``--help`` 显示的名字与实际命令不符；
4. 三个 target 的模块在源码树中真实存在且 ``main`` 可调用。

第 2 条从 ``_EXPECTED`` 的 target 反推子包，而不是写死两个名字：新增命令面时
自动纳入覆盖，``http_server`` / 未来新增的子包不会被漏掉。

只做静态断言与无副作用调用，不构建 wheel（耗时且依赖网络），wheel 内容由第 2 条覆盖。
CI 的 unit job 只装 ``.[dev]``（无 ``mcp`` extra），本文件因此不得依赖 ``mcp`` 可导入。
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import fnmatch
import importlib
import importlib.util
import io
import logging
import pathlib
import tomllib
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# 命令名 → (模块, 属性)。程序名一致性不在这里声明，直接取运行期真实值，
# 避免「声明了却从没被断言」的死数据。
#
# MCP 指向 `_console`（console_scripts 的薄包装），不是 `__main__`：后者在导入期
# 就要求 mcp SDK，装不上 extra 时连 target 都无法解析。
_EXPECTED = {
    "agent-memory": ("jiuwen_memory_entry.cli.__main__", "main"),
    "agent-memory-server": ("jiuwen_memory_entry.http_server.__main__", "main"),
    "agent-memory-mcp": ("jiuwen_memory_entry.mcp_server._console", "main"),
}


def _load_pyproject() -> dict:
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _packages_find() -> dict:
    return _load_pyproject()["tool"]["setuptools"]["packages"]["find"]


def _subpackage_of(module: str) -> str:
    """``jiuwen_memory_entry.cli.__main__`` → ``jiuwen_memory_entry.cli``。

    取到包路径（去掉最后的模块名）。命令面 target 都形如 ``<pkg>.<sub>.__main__``，
    去掉 ``__main__`` 后剩下的前缀即需要进 wheel 的子包。
    """
    return module.rsplit(".", 1)[0]


def test_project_scripts_declares_three_commands() -> None:
    scripts = _load_pyproject()["project"].get("scripts", {})
    assert set(scripts) == set(_EXPECTED), (
        f"[project.scripts] 命令集合漂移：期望 {sorted(_EXPECTED)}，实际 {sorted(scripts)}"
    )
    for command, (module, attr) in _EXPECTED.items():
        assert scripts[command] == f"{module}:{attr}", (
            f"{command} 的 target 应为 {module}:{attr}，实际 {scripts[command]}"
        )


def _top_level_functions(path: pathlib.Path) -> set[str]:
    """返回模块中真实定义的顶层函数名（AST 解析，忽略注释与字符串）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_entry_point_targets_resolve() -> None:
    """每个 target 的模块在源码树中存在，且确实定义了入口函数。

    用 ``find_spec`` 只定位、不执行模块级代码：MCP 入口在导入期要求 ``mcp`` SDK
    并读取 ``sys.argv``，真正 import 会在无 extra 的 CI 里失败。

    ``find_spec`` 的查找按 ``sys.path`` 顺序，若环境里另装了本包，可能命中
    site-packages 而非源码树——那样断言的就不是本次改动。故显式要求 origin
    落在仓库根内。

    函数定义用 AST 判定而非 ``f"def {attr}(" in source``：后者会把注释掉的
    ``# def main(`` 也算作存在。
    """
    for command, (module, attr) in _EXPECTED.items():
        spec = importlib.util.find_spec(module)
        assert spec is not None, f"{command} 指向的模块 {module} 无法解析"
        assert spec.origin is not None, f"{module} 无源码文件，无法验证 {attr}"
        origin = pathlib.Path(spec.origin).resolve()
        assert origin.is_relative_to(_REPO_ROOT), (
            f"{module} 解析到了仓库外的 {origin}，断言的不是本次改动"
        )
        assert attr in _top_level_functions(origin), f"{module} 未定义顶层函数 {attr}"


def test_command_packages_not_excluded_from_wheel() -> None:
    """回归锁：命令面 target 所在的每个子包都不得被 exclude 出 wheel。

    从 ``_EXPECTED`` 反推子包，三个命令面（cli / http_server / mcp_server）以及
    未来新增的都会被覆盖；写死名字会让新增命令面静默失去保护。

    匹配用 ``fnmatchcase`` 而非子串：setuptools 的 ``packages.find`` 正是用
    ``fnmatch.fnmatchcase(包名, 模式)`` 判定（``setuptools/discovery.py``）。子串匹配会
    放过 ``jiuwen_memory_entry.*`` / ``*cli*`` / ``*`` 这类同样能排除该包的写法。
    """
    find = _packages_find()
    exclude = find.get("exclude", [])
    for command, (module, _) in _EXPECTED.items():
        package = _subpackage_of(module)
        hits = [pattern for pattern in exclude if fnmatch.fnmatchcase(package, pattern)]
        assert not hits, (
            f"{command} 依赖的 {package} 会被 exclude 出 wheel：{hits}（实际 exclude={exclude}）"
        )

    include = find.get("include", [])
    assert any(fnmatch.fnmatchcase("jiuwen_memory_entry.x", pattern) for pattern in include), (
        f"include 必须覆盖 jiuwen_memory_entry 子包，实际 {include}"
    )


def _fastmcp_display_name(path: pathlib.Path) -> str | None:
    """取模块中 ``FastMCP(...)`` 调用的第一个位置参数（显示名）。

    用 AST 而非源码字符串匹配：换行、缩进、引号任一变化都会让字符串匹配误报。
    ``_RuntimeFastMCP`` 是 FastMCP 的子类，同一构造签名。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.endswith("FastMCP")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            return str(node.args[0].value)
    return None


def test_mcp_console_reports_missing_sdk_without_traceback() -> None:
    """``_console`` 缺 mcp SDK 时给一行提示 + 退出码 2，且不改动 logger 级别。

    这是 console_scripts 对用户的可见行为：直接指向 ``__main__`` 会让用户看到
    整段 traceback 且退出码为 1。同时锁住「不压制日志」——内核侧的
    ``development authentication is enabled`` 是安全告警，不能被入口包装吞掉。
    """
    console = importlib.import_module("jiuwen_memory_entry.mcp_server._console")
    kernel_logger = logging.getLogger("agent-memory.mcp")
    level_before = kernel_logger.level

    real_import = builtins.__import__

    def _missing_mcp(name: str, *args, **kwargs):
        if name.startswith("mcp.server"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    stderr = io.StringIO()
    with (
        mock.patch("builtins.__import__", side_effect=_missing_mcp),
        contextlib.redirect_stderr(stderr),
    ):
        code = console.main()

    assert code == 2, f"缺 SDK 时应以退出码 2 退出，实际 {code}"
    message = stderr.getvalue()
    assert "pip install" in message, f"提示应给出安装命令，实际：{message!r}"
    assert "Traceback" not in message, "不应向用户展示 traceback"
    assert kernel_logger.level == level_before, (
        f"入口包装不得改动内核 logger 级别（{level_before} → {kernel_logger.level}）"
    )


def test_mcp_console_drops_only_redundant_import_warning() -> None:
    """``_console`` 只过滤重复的 ``required import failed``，其余警告照常放行。

    安全告警（如 ``development authentication is enabled``）必须能到达用户；
    按级别压制会把它一起吞掉，故实现用消息前缀过滤而非改 logger 级别。
    """
    console = importlib.import_module("jiuwen_memory_entry.mcp_server._console")
    drop = console._DropRedundantImportWarning()

    def _record(message: str) -> logging.LogRecord:
        return logging.LogRecord(
            "agent-memory.mcp", logging.WARNING, __file__, 1, message, (), None
        )

    assert not drop.filter(_record("required import failed: mcp.server.fastmcp: No module")), (
        "重复的导入失败日志应被过滤"
    )
    security_warning = "development authentication is enabled; do not use in production"
    assert drop.filter(_record(security_warning)), "安全告警不得被过滤"


def test_command_names_match_entry_point_prog() -> None:
    """命令名必须与入口实际使用的程序名一致。

    逐个命令取 parser 声明的 ``prog``：
    - CLI / HTTP：``build_parser().prog``（两者都已把 parser 构造抽成独立函数）；
    - MCP：``FastMCP`` 构造的第一个位置参数（显示名）。

    直接读 ``prog`` 而非跑 ``--help``：后者会抛 ``SystemExit``，且与
    G.ERR.11「避免使用 SystemExit」冲突。
    """
    cli_main = importlib.import_module("jiuwen_memory_entry.cli.__main__")
    assert cli_main.build_parser().prog == "agent-memory"

    http_main = importlib.import_module("jiuwen_memory_entry.http_server.__main__")
    assert http_main.build_parser().prog == "agent-memory-server"

    # MCP 入口模块级即要求 mcp SDK 并读取 argv/env，即使装了 extra 也不能 import
    # （模块级 _build_server() 会真实装配内核并把 sys.argv[1:] 当配置路径）。
    # 用 AST 读构造参数，无副作用、也不受排版影响。
    mcp_spec = importlib.util.find_spec("jiuwen_memory_entry.mcp_server.__main__")
    assert mcp_spec is not None and mcp_spec.origin is not None
    assert _fastmcp_display_name(pathlib.Path(mcp_spec.origin)) == "agent-memory", (
        "MCP FastMCP 显示名应为 agent-memory"
    )
