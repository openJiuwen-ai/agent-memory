"""ActiveRouter / Routing* — 按 ConfigSource 的 ``*.active`` 在已预装实例间切换。

**次选路径**（S08）：同实现多套 model/key/url 应优先走调用路径晚绑定
（:mod:`config.binding`），不要为此拆多套同构具名实例。

本模块用于异质实现互切（如 hashing ↔ openai）或产品明确要求的实例隔离：
装配期注入多套实现；运行期只改 ``embedder.active`` / ``llm.active`` 等，不经业务 API。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from common.base import PluginType
from common.embedder.base import Embedder
from common.llm.base import LLM
from common.reranker.base import Reranker
from common.type_def import ChatMessage
from config.active import resolve_active_name
from config.config_source import ConfigSource

T = TypeVar("T")


class ActiveRouter(Generic[T]):
    """在已预装 ``instances`` 中按 ``<namespace>.active`` 解析当前实例。

    Args:
        namespace: 如 ``embedder`` / ``llm`` / ``reranker``
        instances: 装配期已创建的具名实例表（注册 ≠ 预装配）
        config_source: 读取 ``<namespace>.active`` 的来源
        default_name: ConfigSource 未设置 active 时的默认实例名（必须已在 instances 中）
    """

    def __init__(
        self,
        *,
        namespace: str,
        instances: dict[str, T],
        config_source: ConfigSource,
        default_name: str,
    ) -> None:
        if not instances:
            raise ValueError(f"{namespace} ActiveRouter 需要至少一个预装实例")
        if default_name not in instances:
            raise ValueError(
                f"{namespace} default_name {default_name!r} 不在 instances 中："
                f"{sorted(instances)}"
            )
        self._namespace = namespace
        self._instances = dict(instances)
        self._config_source = config_source
        self._default_name = default_name

    def get(self) -> T:
        """返回当前 active 对应实例；未知 active 名抛 :class:`ValidationError`。"""
        name = resolve_active_name(
            self._config_source,
            namespace=self._namespace,
            available=tuple(self._instances),
            default=self._default_name,
        )
        return self._instances[name]

    @property
    def active_name(self) -> str:
        """当前解析出的具名实例名（与 :meth:`get` 同一套规则）。"""
        return resolve_active_name(
            self._config_source,
            namespace=self._namespace,
            available=tuple(self._instances),
            default=self._default_name,
        )


class RoutingEmbedder(Embedder):
    """Embedder 门面：每次 ``embed`` / ``dimension`` / ``health`` 委托当前 active 实例。"""

    def __init__(self, router: ActiveRouter[Embedder]) -> None:
        self._router = router

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``EMBEDDER``。"""
        return PluginType.EMBEDDER

    def health(self) -> None:
        """委托当前 active Embedder 探活。"""
        self._router.get().health()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """委托当前 active Embedder 向量化。"""
        return self._router.get().embed(texts)

    def dimension(self) -> int:
        """委托当前 active Embedder 的维度（切换实例后可能变化，调用方需自洽）。"""
        return self._router.get().dimension()


class RoutingLLM(LLM):
    """LLM 门面：每次 ``chat`` / ``health`` 委托当前 active 实例。"""

    def __init__(self, router: ActiveRouter[LLM]) -> None:
        self._router = router

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``LLM``。"""
        return PluginType.LLM

    def health(self) -> None:
        """委托当前 active LLM 探活。"""
        self._router.get().health()

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        """委托当前 active LLM 对话。"""
        return self._router.get().chat(messages, **options)


class RoutingReranker(Reranker):
    """Reranker 门面：每次打分 / 探活委托当前 active 实例。"""

    def __init__(self, router: ActiveRouter[Reranker]) -> None:
        self._router = router

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``RERANKER``。"""
        return PluginType.RERANKER

    def health(self) -> None:
        """委托当前 active Reranker 探活。"""
        self._router.get().health()

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        """委托当前 active Reranker 打分。"""
        return self._router.get().rerank(query, texts)
