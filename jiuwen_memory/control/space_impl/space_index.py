"""主体到空间的反查索引（S09）。

取空间清单靠 ``KVSpaceManager.list`` 遍历 ``kv.scopes()`` 是全库扫描，而候选空间集合
与逐空间求值的 ``list_spaces`` 都在热路径上，因此另建一份按主体反查的索引。

索引是派生数据，语义按「超集」定义：允许多给（候选集虚大，由逐空间判定裁决），不允许
遗漏（漏给表现为空间不可见）。因此写入次序为「主数据先、索引后」，删除次序相反。

索引项落 KV 根 scope 桶，与 ``/spaces/by-id/`` 注册表同源。本类由
:class:`~control.space_impl.kv_space_manager.KVSpaceManager` 内部持有，不单独装配。
"""

from __future__ import annotations

import base64

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.kv import KVStore

_INDEX_PREFIX = "/index/principal/"
_ROOT_SCOPE = Scope()


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _principal_key(scope: Scope) -> str:
    """索引桶键三类：具名 user、具名 agent、组织通配。

    写入方只有归属登记项与成员记录，两者均为单维，因此无双维桶。双维记录不进首版的
    原因在判定侧而非索引侧：两轴求值按 user 维与 agent 维各取最具体的一条再取交，
    一条双维记录在两维上同时命中，取交退化为与自身相交，「两维各自约束」的语义消失。
    """
    if scope.user and scope.agent:
        raise ValidationError("index principal must carry exactly one of user/agent")
    if scope.user:
        return f"u:{scope.org}:{scope.user}"
    if scope.agent:
        return f"a:{scope.org}:{scope.agent}"
    return f"org:{scope.org}"  # 主体两维皆空 = 组织通配成员记录


def _index_key(principal: str, space: str) -> str:
    return f"{_INDEX_PREFIX}{_b64(principal)}/{_b64(space)}"


class SpaceIndex:
    """主体到空间的反查索引读写。"""

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    def add(self, principal: Scope, space: str) -> None:
        """登记一条「主体 → 空间」；幂等：已存在即跳过。"""
        if not space:
            raise ValidationError("space is required")
        key = _index_key(_principal_key(principal), space)
        if self._kv.exists(_ROOT_SCOPE, key):
            return
        self._kv.insert(_ROOT_SCOPE, key, space.encode("utf-8"))

    def remove(self, principal: Scope, space: str) -> None:
        """删除一条「主体 → 空间」；幂等：不存在即跳过。"""
        if not space:
            return
        self._kv.delete(_ROOT_SCOPE, _index_key(_principal_key(principal), space))

    def remove_space(self, space: str) -> int:
        """清理某个空间的全部索引项，返回删除条数。

        用于删除空间：孤儿项只造成候选集虚大，会被逐空间判定挡住，因此清理失败可容忍。
        """
        if not space:
            return 0
        suffix = f"/{_b64(space)}"
        removed = 0
        for key, _ in self._kv.scan(_ROOT_SCOPE, prefix=_INDEX_PREFIX):
            if not key.endswith(suffix):
                continue
            self._kv.delete(_ROOT_SCOPE, key)
            removed += 1
        return removed

    def spaces_for(self, actor: Scope, org: str) -> tuple[str, ...]:
        """三路合并后按空间名字典序去重排序：actor 的 user 桶、agent 桶、组织通配桶。

        actor 带两维时两个桶都取，取并集而非交集——索引只负责不遗漏。
        返回值排序是契约的一部分：候选集的上限截断按序取前 N 项，顺序不稳定即截掉的
        空间随调用漂移。
        """
        spaces: set[str] = set()
        buckets = [f"org:{org}"]
        if actor.user:
            buckets.append(f"u:{org}:{actor.user}")
        if actor.agent:
            buckets.append(f"a:{org}:{actor.agent}")
        for principal in buckets:
            prefix = f"{_INDEX_PREFIX}{_b64(principal)}/"
            for _, raw in self._kv.scan(_ROOT_SCOPE, prefix=prefix):
                spaces.add(raw.decode("utf-8"))
        return tuple(sorted(spaces))
