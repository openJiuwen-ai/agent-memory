# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``SHARD_ROUTED`` 端口的装配期协议（F09 D12 / D13）。

装配器只按**能力声明**取端口（``StoreCapability.SHARD_ROUTED`` /
``SHARD_PLACEMENT``），然后按这里的协议访问自描述信息。这样：

- 装配代码不再 ``getattr(port, "shards" / "locator_name" / ...)`` 逐个探测方法；
- 新增分片包装层只要声明能力并实现协议即可接入，不用改装配器的分支（S09 #7）；
- 核心不出现任何具体实现类型或 target 判定（S09 #9）。

协议只描述**装配期**需要的方法；读写路径仍走各自的 Store 契约。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ShardSetProvider(Protocol):
    """声明 ``SHARD_LOCATOR`` 的端口承诺提供已配置分片集合（只读）。"""

    @property
    def shards(self) -> tuple[str, ...]:
        """已配置分片名（顺序稳定；装配期已校验非空）。"""


@runtime_checkable
class ShardRoutedPort(ShardSetProvider, Protocol):
    """分片路由包装层的自描述接口（装配期只读）。"""

    def locator_name(self) -> str | None:
        """注入的定位端口名；端口本身是定位提供方时返回 ``None``。"""

    def shard_descriptors(self, kind: str = "shard") -> dict[str, dict[str, str]]:
        """每个分片的物理标识；``kind`` 是拓扑登记键（``{capability}:{port_name}``）。"""


@runtime_checkable
class ShardLocatorConsumer(Protocol):
    """需要外部定位端口的包装层：装配器在端口建好后调用一次。"""

    def bind_shard_locator(self, locator: Any) -> None:
        """绑定定位端口（通常是 ``manager.kv(<locator_name>)`` 的授权代理）。"""


@runtime_checkable
class ShardPlacementOwner(ShardRoutedPort, Protocol):
    """还提供拓扑登记/读取的端口（当前为分片 KV 包装层）。"""

    def register_topology(self, descriptors: dict[str, dict[str, str]]) -> None:
        """启动期登记；已登记且物理标识不一致时拒绝启动。"""

    def read_topology(self) -> dict[str, Any]:
        """读取拓扑登记。"""

    def relocation_enabled(self) -> bool:
        """是否开启「改路由字段后自动搬迁」（F09 D16）。"""

    def bind_relocatable_ports(self, ports: tuple[Any, ...]) -> None:
        """装配期绑定声明 ``SHARD_RELOCATABLE`` 的派生端口。"""


@runtime_checkable
class ShardDependencyRebinder(Protocol):
    """按引用持有具名依赖的分片包装层：统一加密包装具名端口后，把引用换成受保护实例。

    ``replacements`` 以原实例 ``id()`` 为键、受保护实例为值；包装层只替换自己确实持有的
    依赖。声明 ``SHARD_ROUTED`` 的 KV / FS 端口必须实现本协议，否则装配器无法保证
    passthrough 等借用依赖不绕过加密，启用统一加密时直接拒绝启动。
    """

    def rebind_dependencies(self, replacements: dict[int, Any]) -> None:
        """把持有的依赖替换为受保护实例。"""
