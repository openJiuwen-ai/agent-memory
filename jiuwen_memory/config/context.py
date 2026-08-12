"""新装配模型：两级命名空间配置 + builder 视图（过渡期与旧 :class:`ComponentSpec` 并存）。

- :class:`RawSpec`：一个**具名实例**的纯数据（``target`` / ``params`` / ``new_instance``）。
- :class:`AssemblyContext`：全局装配上下文——所有命名空间 + ``globals``；
  Producer 经自己的 ``TOP_NAME`` 定位命名空间（``lookup(top_name, name)``）。
- :class:`ComponentConfig`：传给各 ``_build`` 的 ``config`` 视图：本实例 ``params`` +
  回退 ``globals`` + 指回 ``ctx`` 的句柄（供 builder 经 ``XProducer.dep`` 取依赖）。

配置形态（两级命名空间）::

    globals:                 # 跨切面参数；ComponentConfig.get 找不到本实例 params 时回退到这里
      embedder_dim: 64
    kv_store:                # KvProducer.TOP_NAME == "kv_store" → 该命名空间
      main_kv:               # 具名实例（共享键）
        target: redis
        params: { url: "..." }
    constructor:             # IndexBuilderProducer.TOP_NAME == "constructor"
      main_ib:
        target: hybrid
        params:
          vector_store: main_vec   # 字符串 = 引用 vector_store 命名空间下的 main_vec（共享）
          fulltext_store: { target: memory }   # dict = 内联匿名实例（不共享）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from jiuwen_memory.common.errors import ValidationError

# 保留的顶层段名：不作为命名空间解析。globals=跨切面参数；prompts=动态 prompt 文本，
# 进 globals["prompts"] 供 PromptRegistry 加载。
_RESERVED_TOP_NAMES = {"globals", "prompts"}


@dataclass(frozen=True)
class RawSpec:
    """一个具名实例的纯数据：选哪个实现 + 字面参数 + 是否退出共享。"""

    target: str
    params: Dict[str, Any] = field(default_factory=dict)
    new_instance: bool = False


@dataclass
class AssemblyContext:
    """全局装配上下文：所有命名空间（top_name -> name -> RawSpec）+ 跨切面 ``globals``。"""

    globals: Dict[str, Any] = field(default_factory=dict)
    namespaces: Dict[str, Dict[str, RawSpec]] = field(default_factory=dict)

    def lookup(self, top_name: str, name: str) -> RawSpec:
        """取 ``top_name`` 命名空间下名为 ``name`` 的具名实例配置；不存在即报错。"""
        ns = self.namespaces.get(top_name)
        if ns is None or name not in ns:
            known = sorted(ns) if ns else []
            raise ValidationError(
                f"引用的具名配置不存在：{top_name}.{name!r}（{top_name} 已定义：{known}）"
            )
        return ns[name]

    def merged(self, other: "AssemblyContext") -> "AssemblyContext":
        """把 ``other`` 覆盖到本上下文之上：globals 按 key 覆盖、命名空间按实例名覆盖/新增。

        用于 ``build_kernel`` 把用户配置叠加到内置默认之上——用户只需写要改动的部分。
        """
        namespaces: Dict[str, Dict[str, RawSpec]] = {
            top: dict(insts) for top, insts in self.namespaces.items()
        }
        for top, insts in other.namespaces.items():
            namespaces.setdefault(top, {}).update(insts)
        merged_globals = dict(self.globals)
        merged_globals.update(other.globals)
        return AssemblyContext(globals=merged_globals, namespaces=namespaces)

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any] | None,
        *,
        known_top_names: Optional[set[str]] = None,
    ) -> "AssemblyContext":
        """从配置字典解析。``known_top_names`` 非空时校验每个顶层段是已注册的 Producer 顶层名。

        ``globals`` 与 ``prompts`` 是保留段：``globals`` 进跨切面参数；``prompts`` 进
        ``globals["prompts"]`` 供 :class:`~construction.prompt_registry.PromptRegistry` 加载。
        """
        data = data or {}
        globals_ = dict(data.get("globals", {}) or {})
        prompts = data.get("prompts")
        if prompts is not None:
            globals_["prompts"] = prompts
        namespaces: Dict[str, Dict[str, RawSpec]] = {}
        for top_name, section in data.items():
            if top_name in _RESERVED_TOP_NAMES:
                continue
            if known_top_names is not None and top_name not in known_top_names:
                raise ValidationError(
                    f"未知的顶层配置段 {top_name!r}"
                    f"（已注册 Producer 顶层名：{sorted(known_top_names)}）"
                )
            if not isinstance(section, Mapping):
                raise ValidationError(
                    f"配置段 {top_name!r} 应是 名称->实例配置 的映射，得到 {type(section).__name__}"
                )
            namespaces[top_name] = {
                inst_name: _parse_instance(top_name, inst_name, raw)
                for inst_name, raw in section.items()
            }
        return cls(globals=globals_, namespaces=namespaces)


def _parse_instance(top_name: str, inst_name: str, raw: Any) -> RawSpec:
    """把一个具名实例配置（字符串简写或含 target 的映射）解析成 :class:`RawSpec`。"""
    if isinstance(raw, str):  # 简写：name: target
        return RawSpec(target=raw)
    if not isinstance(raw, Mapping):
        raise ValidationError(
            f"{top_name}.{inst_name!r} 应是实现名字符串或含 'target' 的映射，"
            f"得到 {type(raw).__name__}"
        )
    target = raw.get("target")
    if not target:
        raise ValidationError(f"{top_name}.{inst_name!r} 缺少 'target'（写实现名）")
    return RawSpec(
        target=str(target),
        params=dict(raw.get("params", {}) or {}),
        new_instance=bool(raw.get("new_instance", False)),
    )


@dataclass
class ComponentConfig:
    """传给各 ``_build`` 的配置视图：本实例 ``params`` + 回退 ``globals`` + ``ctx`` 句柄。"""

    params: Dict[str, Any]
    ctx: AssemblyContext
    target: str = ""
    name: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        """读参数：本实例 ``params`` 优先，缺失则回退 ``ctx.globals``，最终给 ``default``。"""
        if key in self.params:
            return self.params[key]
        if key in self.ctx.globals:
            return self.ctx.globals[key]
        return default
