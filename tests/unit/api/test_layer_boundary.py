"""API 层的分层边界（S02「不管什么」）。

判据落在测试里而不只落在规约里：分层是靠人工核对维持的约定，一次疏忽即成既成事实，
而它不会使任何功能用例失败。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_API_IMPL = Path(__file__).resolve().parents[3] / "jiuwen_memory" / "api" / "memory_api_impl"

# 装配代码不在此约束内：import 各层 Producer 完成组装正是它的职责，
# S02 的边界管的是 MemoryAPI 实现的运行时行为。
_ASSEMBLY_FILES = {"assembly.py", "__init__.py"}

# 构建层导出的类型与无状态纯函数，按 S02「不调用 LLM / 构建 / 检索」的判据不属算子：
# 无 Producer 注册、实现不可替换、不访问模型或存储。
_ALLOWED_CONSTRUCTION_CALLS = {
    "EvolveMode",  # 枚举
    "RouteContext",  # 数据类
    "RouteDecision",  # 数据类
    "NarrowDim",  # 数据类
    "SpaceNaming",  # 数据类
    "degraded_reasons",  # 纯计数：判定结果里非原样落点的条目按 reason 归并
    "fill_missing_tag_keys",  # 纯 dict 变换，落盘不变量的一半
    "narrow_dims_of",  # 纯 dict 变换：归属坐标折算成收窄维取值
    "reject_kernel_coords",  # 入口校验：拒绝调用方给内核三项坐标赋值
}

# 检索层导出的类型与无状态纯函数，判据与上一个允许集同一条。
# 本集合按实扫结果给出，不预留：新增项须逐个对照判据，不得因「看起来同类」直接加。
#
# 取数上界（``allocate_quota``）与结果合并（``merge``）曾在本集合内——跨空间召回的扇出
# 编排下沉到 ``control/collective/cross_space_recall.py`` 之后，调用它们的是控制层，本层
# 不再直接调用，两项随之摘除。剩下的 ``space_error`` 是本层构造「某个空间整体没进结果」
# 那条错误项时用的纯构造函数：判权剔除在本层、扇出失败在控制层，两处共用一个构造点，
# 各写一份即 ``channel`` / ``source`` 的编码在两层各有一份。
_ALLOWED_RETRIEVAL_CALLS = {
    "RetrievalQuery",  # 数据类
    "RetrievalResult",  # 数据类
    "space_error",  # 纯构造，不访问存储
}


_CONSTRUCTION_PKG = "jiuwen_memory.construction"
_RETRIEVAL_PKG = "jiuwen_memory.retrieval"


def _dotted(node: ast.expr) -> str:
    """把 ``a.b.c`` 形态的表达式还原为点号全名；非纯属性链返回空串。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _layer_calls(path: Path, package: str) -> set[str]:
    """该文件里被实际调用的、来自 ``package`` 的名字。

    三种引用形态都要覆盖，漏掉任何一种都留下一条绕过本约束的写法：

    | 形态 | 调用写法 |
    |---|---|
    | ``from ...router import route_batch`` | ``route_batch()`` |
    | ``import jiuwen_memory.construction.router [as r]`` | ``r.route_batch()`` |
    | ``from jiuwen_memory.construction import router`` | ``router.route_batch()`` |

    第三种形态下绑定的名字是子模块，调用写作 ``子模块.属性``——按名字直接比对匹配不到
    ``route_batch``，因此该名字同时登记为属性链前缀。类型名以同样方式登记，其上的属性
    调用（如 ``RouteDecision.from_x()``）也随之纳入扫描，与逐个开例外的判据一致。
    """
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    imported: set[str] = set()
    module_prefixes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(package):
            names = {alias.asname or alias.name for alias in node.names}
            imported.update(names)
            # 绑定的可能是子模块（第三种形态），此时调用写作 ``子模块.属性``，
            # 按名字比对匹配不到被调函数，故同时登记为属性链前缀。
            module_prefixes.update(names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(package):
                    # 无 asname 时绑定的是根包名，调用写作完整点号路径
                    module_prefixes.add(alias.asname or alias.name)
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in imported:
                called.add(func.id)
            continue
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr in imported:
            called.add(func.attr)
            continue
        dotted = _dotted(func)
        if any(dotted.startswith(f"{prefix}.") for prefix in module_prefixes):
            called.add(func.attr)
    return called


def test_the_api_layer_does_not_call_construction_operators() -> None:
    """API 层不调用构建层算子。

    结论直写路径确实需要归属判定，但那次调用落在控制层的 ``control/collective/routing.py``，
    本层调控制层。允许集只含该层导出的类型与无状态纯函数——判据是「无 Producer 注册、
    实现不可替换、不访问模型或存储」，不是逐个开例外。

    这条边界有牙才有意义：一旦为某个算子破例，后续任何「API 想直调某个构建算子」的改动
    都会援引它。
    """
    offenders: dict[str, set[str]] = {}
    for path in sorted(_API_IMPL.glob("*.py")):
        if path.name in _ASSEMBLY_FILES:
            continue
        extra = _layer_calls(path, _CONSTRUCTION_PKG) - _ALLOWED_CONSTRUCTION_CALLS
        if extra:
            offenders[path.name] = extra
    assert not offenders, f"API 层直接调用了构建层算子: {offenders}"


def test_the_api_layer_does_not_call_retrieval_operators() -> None:
    """API 层不调用检索层算子。

    与上一条同判据、同扫描逻辑，只换包名。两侧对称是有意的：S02「不调用 LLM / 构建 /
    检索」同时管这两层，只守构建层即留下一个无用例守护的调用面——跨空间检索把取数上界与
    结果合并落在检索层之后，该面上确实有调用。
    """
    offenders: dict[str, set[str]] = {}
    for path in sorted(_API_IMPL.glob("*.py")):
        if path.name in _ASSEMBLY_FILES:
            continue
        extra = _layer_calls(path, _RETRIEVAL_PKG) - _ALLOWED_RETRIEVAL_CALLS
        if extra:
            offenders[path.name] = extra
    assert not offenders, f"API 层直接调用了检索层算子: {offenders}"
