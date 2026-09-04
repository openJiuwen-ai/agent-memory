"""文档记忆开关 ``globals.write_document`` 的 key 常量与归一函数。

- 常量 :data:`WRITE_DOCUMENT_KEY`：globals 下的 key 名，与 ``vector_enabled`` 等同级。
- :func:`should_write_document`：把装配期 ``config.get(WRITE_DOCUMENT_KEY)`` 取到的
  原始值（可能是 ``bool`` 或 ``"true"/"false"`` 字符串）归一为 ``bool``。

各消费方（storage/construction/control）在自己的 ``_build`` 装配期调用本函数归一，
把结果**固化进实例属性**（如 ``self._write_document``）；运行期方法直接读实例属性，
不再持有 config 句柄——与 ``CompositeStorage._preferred_pipeline`` 等现有开关同范式。

与 :mod:`jiuwen_memory.config.active` 的分工：active 解析 ConfigSource 晚绑定；
本模块只做装配期开关归一，不碰晚绑定。
"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import ValidationError

# globals 下的 key 名（``globals.write_document``）。
WRITE_DOCUMENT_KEY = "write_document"

# globals 下的 key 名（``globals.watch_document``）：文档看门狗启停开关。
# 仅 ``write_document=true`` 下有意义（无文档真源即无 md 可监听）；默认随 write_document
# —— 即未配置时取 True（已开文档就该 watch）。与 WRITE_DOCUMENT_KEY 的差异仅在 None 语义：
# write_document 未配=False（默认不写文档）；watch_document 未配=True（默认随文档开启）。
WATCH_DOCUMENT_KEY = "watch_document"

_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


def should_write_document(raw: Any) -> bool:
    """把 ``write_document`` 原始值归一为布尔。

    接受：

    - ``bool``：原样返回。
    - ``"true"/"false"``（及常见变体 ``1/0``、``yes/no``、``on/off``，大小写不敏感）：
      归一到对应布尔。
    - ``None``：视为未配置，回退默认 ``False``（与 :data:`defaults.WRITE_DOCUMENT_KEY`
      的默认值一致——仅写 KV，不写文档）。

    不识别的值抛 :class:`~jiuwen_memory.common.errors.ValidationError`，装配期 fail-closed，
    不静默回退到 False（避免 ``write_document: yes`` 之类拼写错误被吞成关闭文档路径）。

    调用位置：各消费方 ``_build`` 里
    ``should_write_document(config.get(WRITE_DOCUMENT_KEY, False))``。
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        # 数值型：0=False，非零=True。兼容 YAML 里偶发的裸数字写法。
        return bool(raw)
    text = str(raw).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    raise ValidationError(
        f"globals.{WRITE_DOCUMENT_KEY}={raw!r} 无法归一为布尔；"
        f"预期 true/false（或 {sorted(_TRUE_STRINGS | _FALSE_STRINGS)}）"
    )


def resolve_index_builder_default(config: Any) -> str:
    """按文档模式 / 向量开关算出 ``IndexBuilder`` 的缺省实现名。

    装配期各 ``_build`` 在 params 未显式声明 ``index_builder`` 字段时，``dep`` 走缺省
    分支用本函数给出的实现名匿名新建——**三处消费方（engine / evolver / job_factory）
    必须共用本函数**，否则缺省判定分叉会让同一份装配拿到不一致的 IndexBuilder：

    文档模式（``write_document=true``）必须取 ``document``（全委托 ``storage.add``
    文档分流写 md + 影子索引，不碰 KV）；非文档模式随 ``vector_enabled`` 在
    ``hybrid``（向量 + 倒排）/ ``fulltext``（仅倒排）间择一。

    若调用方在 params **显式**写了 ``index_builder`` 字段（引用或内联），``dep`` 走
    引用 / 内联分支、本函数的返回值不会被用上——但那要求引用指向的具名实例确实与
    文档模式相容（文档模式下指向 ``constructor.default`` 而 ``target=hybrid`` 即错配，
    需在配置侧把 target 改成 ``document``）。
    """
    if should_write_document(config.get(WRITE_DOCUMENT_KEY, False)):
        return "document"
    if config.get("vector_enabled", True):
        return "hybrid"
    return "fulltext"


def resolve_watch_document(raw: Any) -> bool:
    """把 ``watch_document`` 原始值归一为布尔。

    与 :func:`should_write_document` 共用字符串/数值归一，**唯独 None 语义不同**：
    ``None``（未配置）→ ``True``——默认随 :func:`should_write_document` 返回 ``True``
    的文档模式开启看门狗（开了文档就该监听 md 漂移）。已显式配成 ``true/false`` 时
    原样返回，拼写错误同样 fail-closed 抛 :class:`ValidationError`。

    调用位置：``build_kernel`` 装配期，仅在 ``write_document=true`` 后判断是否装配
    并启动看门狗。``write_document=false`` 时本开关无意义（不进文档模式）。
    """
    if raw is None:
        return True  # 未配置默认开（随文档）
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    raise ValidationError(
        f"globals.{WATCH_DOCUMENT_KEY}={raw!r} 无法归一为布尔；"
        f"预期 true/false（或 {sorted(_TRUE_STRINGS | _FALSE_STRINGS)}）"
    )
