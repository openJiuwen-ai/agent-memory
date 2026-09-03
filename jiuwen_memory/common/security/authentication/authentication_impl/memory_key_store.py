# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""进程内 :class:`~common.security.authentication.key_store.PrincipalKeyStore`，Argon2id 校验。

**已知限制（两条，均在归档文档「已知遗留」列明）**：

1. **性能**：Argon2id 128 MiB × time_cost=4 的单次 verify 在典型硬件上
   50~200ms，意味着 API 吞吐上限约 5~20 QPS/核。第一期**不做验证缓存**——
   缓存会带来撤销延迟（撤销后缓存内 key 仍有效 = 安全漏洞）这个新的安全问题，
   在没有生产流量的阶段不值得引入。高 QPS 场景需要带撤销传播的缓存。
2. **持久化**：进程重启后所有已签发的 key 失效。生产需要 SQLite 后端。

注册名是 ``memory`` 而非 ``argon2``：Argon2 描述的是**哈希算法**（内部细节），
``memory`` 描述的是**存储后端**，与主干 ``sqlite_permission_manager`` /
``in_memory_governor`` 的命名惯例一致。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.authentication.key_store import (
    KeyStoreProducer,
    PrincipalKeyStore,
    fingerprint,
    generate_api_key,
    key_prefix,
)
from jiuwen_memory.common.security.types import AuthContext, Role
from jiuwen_memory.common.type_def.scope import Scope

# Argon2id 参数（OWASP 2024+，security.md §2.3.1）。
# 显式指定全部五项，不用库默认——argon2-cffi 的默认 memory_cost 是 64 MiB，
# 低于 OWASP 推荐，且默认值随版本变化。
_TIME_COST = 4
_MEMORY_COST = 131072  # 128 MiB
_PARALLELISM = 2
_HASH_LEN = 32
_SALT_LEN = 16

_DUMMY_KEY = "dummy-key-for-timing-pad"

_CREDENTIAL = "api_key"  # 本注册表签发的凭据类型；认证方法名由认证实现补齐


@dataclass
class _Record:
    """一条主体 key 记录。**不含明文**——明文只在 issue 时返回一次。"""

    key_fp: str
    key_hash: str
    actor: Scope
    role: Role
    revoked: bool = False


class InMemoryKeyStore(PrincipalKeyStore):
    """进程内注册表 + Argon2id 校验。

    ``hasher`` 与异常类型由 ``_build`` 注入：argon2-cffi 是可选依赖，模块顶层
    import 会让缺依赖的环境连 DEV 模式都起不来（``register_plugins()`` 无差别
    import 全部实现包）。见 ``_build``。
    """

    def __init__(self, hasher: Any, mismatch_errors: tuple[type[BaseException], ...]) -> None:
        self._hasher = hasher
        self._mismatch_errors = mismatch_errors
        self._records: dict[str, _Record] = {}  # key_fp -> record
        self._prefix_index: dict[str, list[str]] = {}  # key 前缀 -> [key_fp]
        # role 按 **principal**（org + user/agent）索引，不含 space、不含 session：
        # §3.1 角色是 principal 级（一个 user 是 USER/ADMIN/ROOT，不随 space 或 session
        # 变）。含 space 会让「同 principal 不同 space 同 role」变成两条互覆记录；
        # 含 session 会让「同一 principal 换个 session 登录」查不到 role。两者都是
        # 把非身份维度塞进了身份索引。
        self._roles: dict[tuple[str, str, str], Role] = {}  # (org, user, agent) -> role
        # 状态锁：issue 的「检查 role -> hash -> 写 record/role」、revoke 的「标记撤销
        # -> 重算 role」、resolve 的「取候选 -> 确认未撤销」都必须原子（验收复验 P2-role：
        # 否则两线程并发 issue 不同 role，都看到 existing=None，最终 _records 同时存在
        # USER/ADMIN 而 _roles 只留最后写入者）。RLock 因 resolve 在锁内调 _verify 之外
        # 不需要重入，但 revoke/get_role 可能被同链路调用，RLock 更稳。
        self._lock = threading.RLock()
        # dummy 哈希供 resolve 未命中时 pad 时间。装配期算一次（约 100ms），
        # 之后每次 resolve 复用。
        self._dummy_hash: str = hasher.hash(_DUMMY_KEY)

    # -- 内部 ------------------------------------------------------------ #

    @staticmethod
    def _role_key(actor: Scope) -> tuple[str, str, str]:
        """principal 级 role 索引键：(org, user, agent)。

        Scope 是可变 dataclass（unhashable），不能直接作 dict key。这里只取身份
        维度（§3.1：role 是 principal 级），不含 space / session--见 ``_roles`` 注释。
        """
        return (actor.org, actor.user, actor.agent)

    def _verify(self, stored_hash: str, provided: str) -> bool:
        """常时间校验。

        只捕获 argon2 的校验类异常：未预期的异常（如内存不足）应该炸出来，
        静默 ``return False`` 会把系统性故障伪装成认证失败。
        """
        try:
            return bool(self._hasher.verify(stored_hash, provided))
        except self._mismatch_errors:
            return False

    # -- 契约 ------------------------------------------------------------ #

    def issue(self, actor: Scope, role: Role) -> str:
        if role is Role.ROOT:
            # §3.2「明确禁止」：ROOT 只能来自配置声明的 Root API Key。
            raise PermissionDeniedError("issue", message="cannot issue a ROOT key")
        if bool(actor.user) == bool(actor.agent):
            # §4.1：同一个归属 scope 不应同时设置 user 与 agent；也不能都不设，
            # 否则签出的是「整个 org」这种无主体的 key。
            raise ValidationError("principal scope must set exactly one of user / agent")
        if not actor.org:
            raise ValidationError("principal scope must set org")

        # role 是 principal 的唯一权威状态（§3.1），不是每把 key 的可冲突副本：
        # 同 principal 已有不同 role 的有效 key 时拒绝签发（审计验收 P2-role）。
        # 否则 issue 覆盖 _roles 后，resolve（读 record.role）与 get_role（读 _roles）
        # 会返回不一致；revoke ADMIN key 后 _roles 仍残留 ADMIN = 撤销后提权残留。
        # 换 role 须先 revoke 该 principal 全部 key，或走专门的 set_role（本期未提供）。
        #
        # 并发原子性（验收复验 P2-role）：Argon2 hash（~200ms）在锁**外**算，进锁后
        # **重新检查** principal role 再原子提交 record/index/role。否则两线程并发
        # issue 不同 role，都看到 existing=None，最终 _records 同时存在 USER/ADMIN。
        api_key = generate_api_key()
        key_fp = fingerprint(api_key)
        key_hash = self._hasher.hash(api_key)  # 昂贵，锁外算
        role_key = self._role_key(actor)
        with self._lock:
            existing = self._roles.get(role_key)
            if existing is not None and existing is not role:
                raise ValidationError(
                    f"principal 已持有 role={existing.value}，签发不同 role={role.value} 前须先 "
                    f"revoke 其全部 key"
                )
            # 检查通过 -> 原子提交三者。actor 存**副本**：``Scope`` 是普通可变
            # dataclass，直接存引用等于让调用方在签发后改 org 就能改掉已签发 key 的
            # 身份（`store.issue(actor, ...)` 之后 `actor.org = "victim"`）。
            self._records[key_fp] = _Record(
                key_fp=key_fp,
                key_hash=key_hash,
                actor=replace(actor),
                role=role,
            )
            self._prefix_index.setdefault(key_prefix(api_key), []).append(key_fp)
            self._roles[role_key] = role
        return api_key

    def resolve(self, api_key: str) -> AuthContext | None:
        # 并发契约（验收复验 P2-role）：不在锁内跑完整 Argon2（~200ms，会串行化所有
        # 认证）。先锁内取候选快照，锁外 verify，命中后再锁内确认记录未被撤销。
        with self._lock:
            candidates = [
                self._records.get(key_fp)
                for key_fp in self._prefix_index.get(key_prefix(api_key), ())
            ]
            candidates = [r for r in candidates if r is not None and not r.revoked]
        verified_any = False
        for record in candidates:
            verified_any = True
            if self._verify(record.key_hash, api_key):
                # 命中：锁内确认记录仍存在且未撤销（revoke 可能在这期间发生）
                with self._lock:
                    current = self._records.get(record.key_fp)
                    if current is None or current.revoked:
                        continue
                    return AuthContext(
                        actor=replace(current.actor),
                        role=current.role,
                        credential_type=_CREDENTIAL,
                        credential_id=record.key_fp,
                    )

        # 无候选时补一次 dummy verify，把耗时 pad 到与「有候选」路径同量级。
        # 少了它，「前缀不存在」比「前缀存在但 key 错」快一整个 Argon2 verify
        # （~200ms），可用来枚举有效 key 前缀（§2.3.2）。
        #
        # 条件是 `not verified_any` 而非无条件：无条件 pad 会让「有候选但 key 错」
        # 跑两次 verify，反而造出一个反向的 2x 时间差--同样是可测量的侧信道。
        # 三条路径（命中 / 有候选未命中 / 无候选）都恰好一次 verify 才是对的。
        if not verified_any:
            self._verify(self._dummy_hash, api_key)
        return None

    def revoke(self, key_fp: str) -> None:
        with self._lock:
            record = self._records.get(key_fp)
            if record is None:
                return  # 幂等
            record.revoked = True
            # 按剩余有效 key 重算 role（审计验收 P2-role）：此前「还有任意有效 key 就保留
            # 当前 _roles」不重算，会残留被撤销 key 的 role。现在取剩余有效 key 的 role--
            # 因 issue 已禁止同 principal 不同 role，剩余 key 的 role 恒与被撤销的一致，
            # 但重算使「先 revoke ADMIN 再 revoke USER」等顺序无关。无剩余 key 则清空。
            key = self._role_key(record.actor)
            remaining = [
                r
                for r in self._records.values()
                if not r.revoked and self._role_key(r.actor) == key
            ]
            if remaining:
                self._roles[key] = remaining[0].role
            else:
                self._roles.pop(key, None)

    def is_revoked(self, credential_id: str) -> bool:
        # credential_id 即 issue 时算的 key 指纹；空串（未走可撤销凭据的认证路径）
        # 直接返回 False，不查表。
        if not credential_id:
            return False
        with self._lock:
            record = self._records.get(credential_id)
            return record is not None and record.revoked

    def get_role(self, actor: Scope) -> Role | None:
        with self._lock:
            return self._roles.get(self._role_key(actor))

    def health(self) -> None:
        return None


# -- 注册到 KeyStoreProducer ------------------------------------------------ #


@KeyStoreProducer.register("memory")
def _build(config):
    """装配 InMemoryKeyStore。

    argon2-cffi 的 import 在**这里**而非模块顶层：``register_plugins()`` 会
    无差别 import 整个 ``authentication_impl`` 包，顶层 import 会让缺依赖的环境连
    DEV 模式都起不来。挪进 builder 后，注册总能成功，只有真正装配本实现时才
    要求依赖，且失败是装配期的清晰 ``ValidationError``。

    **绝不回退到明文比对**：加密层也关闭时 key 就是磁盘上的裸明文。fail-closed。
    """
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    except ImportError as exc:
        # 区分两种情况给运维可操作的诊断：
        # - argon2 包本身没装 -> 装 extra；
        # - 包装了但版本太旧（如 21.3.0 没有 InvalidHashError）-> 升级到 >=23.1。
        # 两者都是 ImportError（子模块存在但缺名字也抛 ImportError），靠 argon2 顶层
        # 能否 import 区分。
        try:
            import argon2  # noqa: F401
        except ImportError:
            raise ValidationError(
                "key_store 'memory' 需要 argon2-cffi：pip install 'JiuwenMemory[security]'。"
                "不回退到明文比对--那会让 key 变成磁盘上的裸明文。"
            ) from exc
        # argon2 顶层能 import 但 from ... import 失败 = 版本过旧（如 21.3.0
        # 无 InvalidHashError）。区分两路径给运维可操作诊断（审计验收 P1-uv.lock）。
        raise ValidationError(
            "key_store 'memory' 需要 argon2-cffi>=23.1（当前版本过旧，缺少"
            " InvalidHashError）：升级 pip install 'JiuwenMemory[security]' --upgrade。"
            "不回退到明文比对。"
        ) from exc

    hasher = PasswordHasher(
        time_cost=_TIME_COST,
        memory_cost=_MEMORY_COST,
        parallelism=_PARALLELISM,
        hash_len=_HASH_LEN,
        salt_len=_SALT_LEN,
    )
    # VerifyMismatchError 是正常的「key 错」路径；InvalidHashError /
    # VerificationError 是哈希损坏或参数不符 → 同样 fail-closed 判为不通过。
    return InMemoryKeyStore(
        hasher=hasher,
        mismatch_errors=(VerifyMismatchError, InvalidHashError, VerificationError),
    )
