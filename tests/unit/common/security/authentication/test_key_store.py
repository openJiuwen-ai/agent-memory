"""common.security.authentication.key_store: 签发、解析、撤销、常时间与「不存明文」回归防线。"""

from __future__ import annotations

# The plaintext-retention assertion must inspect the in-memory registry directly.
# pylint: disable=protected-access
import json
import time
from statistics import median

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.authentication.key_store import (
    KeyStoreProducer,
    fingerprint,
    generate_api_key,
)
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.config.context import AssemblyContext

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def store():
    """module 作用域：Argon2 的 dummy hash 每次构造约 200ms，不必每条测试重算。"""
    register_plugins()
    return KeyStoreProducer.build("memory", {}, AssemblyContext())


# -- issue ------------------------------------------------------------------ #


def test_cannot_issue_root_key(store) -> None:
    """§3.2 明确禁止：ROOT 只能来自配置声明的 Root API Key。"""
    with pytest.raises(PermissionDeniedError):
        store.issue(Scope(org="acme", user="alice"), Role.ROOT)


@pytest.mark.parametrize(
    "actor",
    [
        Scope(org="acme"),  # 既非 user 也非 agent
        Scope(org="acme", user="alice", agent="a1"),  # 两者都有
        Scope(user="alice"),  # 无 org
    ],
)
def test_issue_rejects_malformed_principal_scope(store, actor) -> None:
    with pytest.raises(ValidationError):
        store.issue(actor, Role.USER)


def test_issued_keys_are_high_entropy_and_unique(store) -> None:
    keys = {store.issue(Scope(org="acme", user=f"u{i}"), Role.USER) for i in range(5)}
    assert len(keys) == 5
    assert all(len(k) == 43 for k in keys)  # token_urlsafe(32) → 256 bit


def test_generate_api_key_is_unique() -> None:
    assert len({generate_api_key() for _ in range(100)}) == 100


# -- resolve ---------------------------------------------------------------- #


def test_resolve_returns_bound_identity(store) -> None:
    key = store.issue(Scope(org="acme", user="alice"), Role.ADMIN)
    ctx = store.resolve(key)
    assert ctx is not None
    assert ctx.actor == Scope(org="acme", user="alice")
    assert ctx.role is Role.ADMIN
    assert ctx.credential_id == fingerprint(key)


def test_resolve_misses_on_wrong_key(store) -> None:
    store.issue(Scope(org="acme", user="wrong-key-probe"), Role.USER)
    assert store.resolve(generate_api_key()) is None


def test_resolve_does_not_raise_on_garbage(store) -> None:
    for garbage in ("", "x", "中文密钥", "a" * 500):
        assert store.resolve(garbage) is None


# -- revoke ----------------------------------------------------------------- #


def test_revoke_takes_effect_immediately_and_is_idempotent(store) -> None:
    actor = Scope(org="acme", user="revoked-user")
    key = store.issue(actor, Role.USER)
    assert store.resolve(key) is not None

    store.revoke(fingerprint(key))
    assert store.resolve(key) is None
    assert store.get_role(actor) is None

    store.revoke(fingerprint(key))  # 幂等
    store.revoke("nonexistent-fingerprint")


# -- get_role --------------------------------------------------------------- #


def test_get_role_backs_trusted_mode(store) -> None:
    """TRUSTED 模式据此实现「role 不从 header 读」。"""
    actor = Scope(org="acme", agent="gateway-bot")
    assert store.get_role(actor) is None
    store.issue(actor, Role.ADMIN)
    assert store.get_role(actor) is Role.ADMIN


def test_role_is_principal_scoped_not_session_scoped(store) -> None:
    """role 按 principal 索引（§3.1），不含 session。

    同一 principal 换 session 登录仍应查到同一 role；session 进 role_key 会让
    TRUSTED 的 get_role（actor 来自网关、不带 session）查不到已注册主体。
    """
    actor = Scope(org="acme", user="sess-user")
    key = store.issue(actor, Role.USER)

    # 网关声明的 actor 不带 session，但能查到 role
    assert store.get_role(Scope(org="acme", user="sess-user")) is Role.USER
    store.revoke(fingerprint(key))


def test_revoking_one_key_keeps_role_for_other_keys_of_same_principal(store) -> None:
    """同 principal 多 key 共用一个 role 条目：revoke 一把不能让另一把失效。

    回归审计 P2-2：此前 revoke 无条件 pop ``_roles``，导致同 principal 的其它
    有效 key 一起失去角色。
    """
    actor = Scope(org="acme", user="multi-key")
    key_a = store.issue(actor, Role.USER)
    key_b = store.issue(actor, Role.USER)  # 同 role，允许多 key

    store.revoke(fingerprint(key_a))
    # key_b 仍有效，role 仍在
    assert store.resolve(key_b) is not None
    assert store.get_role(actor) is Role.USER

    store.revoke(fingerprint(key_b))
    assert store.get_role(actor) is None


def test_issue_rejects_conflicting_role_for_same_principal(store) -> None:
    """审计验收 P2-role：同 principal 已有不同 role 的 key 时拒绝签发。

    role 是 principal 唯一权威状态，不是每把 key 的可冲突副本。否则 issue 覆盖
    _roles 后 resolve（读 record.role）与 get_role（读 _roles）返回不一致。
    """
    actor = Scope(org="acme", user="role-conflict")
    key = store.issue(actor, Role.USER)
    try:
        with pytest.raises(ValidationError):
            store.issue(actor, Role.ADMIN)
    finally:
        store.revoke(fingerprint(key))
    # revoke 全部后可重新签发不同 role
    store.issue(actor, Role.ADMIN)
    assert store.get_role(actor) is Role.ADMIN


def test_revoke_recomputes_role_order_independent(store) -> None:
    """审计验收 P2-role：revoke 按剩余有效 key 重算 role，与撤销顺序无关。

    覆盖「USER+ADMIN 两 key 分别按两种顺序撤销」--但 issue 禁止同 principal 不同
    role，故此处验证同 role 多 key 的撤销：revoke 任一把，剩余 key 的 role 仍在；
    revoke 全部后 role 清空。重点是不再有「残留被撤销 key 的 role」。
    """
    actor = Scope(org="acme", user="revoke-order")
    key_a = store.issue(actor, Role.USER)
    key_b = store.issue(actor, Role.USER)  # 同 role，允许多 key

    # revoke 一把，另一把仍撑住 role
    store.revoke(fingerprint(key_a))
    assert store.get_role(actor) is Role.USER
    assert store.resolve(key_b) is not None

    # revoke 第二把，role 清空
    store.revoke(fingerprint(key_b))
    assert store.get_role(actor) is None


def test_concurrent_issue_conflicting_role_is_atomic(store) -> None:
    """验收第三次 P3：两线程并发为同 principal 签 USER/ADMIN，恰好一个成功一个冲突。

    严格断言（审计第三次）：join(timeout) 确认线程退出、捕获非 ValidationError 异常
    上抛、结果数 2 / 成功 1 / 冲突 1。实现本身经审计 20 轮强制同拍攻击验证。
    """
    import threading

    actor = Scope(org="acme", user="race-principal-strict")
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def attempt(role):
        try:
            barrier.wait(timeout=5)  # 两线程同时通过，最大化竞态窗口
        except threading.BrokenBarrierError as exc:
            errors.append(exc)
            return
        try:
            key = store.issue(actor, role)
            results.append(("ok", role, key))
        except ValidationError:
            results.append(("conflict", role, None))
        except Exception as exc:  # 非 ValidationError 不该发生，上抛
            errors.append(exc)

    t1 = threading.Thread(target=attempt, args=(Role.USER,))
    t2 = threading.Thread(target=attempt, args=(Role.ADMIN,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive(), "线程未在限时内退出"
    assert not errors, f"非预期异常: {errors}"

    # 恰好一个成功、一个冲突
    assert len(results) == 2, f"结果数应为 2，得到 {results}"
    successes = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(successes) == 1, f"应恰好一个成功，得到 {results}"
    assert len(conflicts) == 1, f"应恰好一个冲突，得到 {results}"

    # _roles 与 _records 一致：_roles 是赢家的 role
    winner_role = successes[0][1]
    assert store.get_role(actor) is winner_role
    # 清理
    store.revoke(fingerprint(successes[0][2]))
    assert store.get_role(actor) is None


def test_issued_identity_is_immutable_to_original_scope_mutation(store) -> None:
    """签发后改原 actor，不影响 resolve 出来的身份。

    ``Scope`` 是普通可变 dataclass，store 存的若是同一个引用，调用方在 issue 之后
    把 ``actor.org`` 改成受害者 org，已签发 key 就跟着换了主体——一次越权。store
    存副本即断掉这条线。
    """
    actor = Scope(org="tenant-a", user="alice")
    key = store.issue(actor, Role.USER)
    actor.org = "tenant-victim"
    ctx = store.resolve(key)
    assert ctx.actor.org == "tenant-a"
    assert ctx.actor.user == "alice"
    store.revoke(fingerprint(key))


def test_resolved_auth_context_actor_mutation_does_not_leak_back(store) -> None:
    """改 resolve 出来的 actor 不回写 store：下一次 resolve 仍是原身份。

    ``AuthContext`` 自身 frozen 只挡住换掉整个 actor，挡不住改 actor 的字段。请求
    处理链上任何一处 ``ctx.actor.org = ...`` 都不该污染注册表里的主体。
    """
    actor = Scope(org="acme", user="immutable-ctx-probe")
    key = store.issue(actor, Role.USER)
    first = store.resolve(key)
    first.actor.org = "tenant-attacker"
    first.actor.user = "bob"

    second = store.resolve(key)
    assert second.actor.org == "acme"
    assert second.actor.user == "immutable-ctx-probe"
    store.revoke(fingerprint(key))


def test_role_does_not_partition_by_space(store) -> None:
    """§3.1 role 是 principal 级，不按 space 分：同 principal 不同 space 同 role。

    审计 P2-2 建议给 role_key 加 space；但 §3.1 角色是 principal 级（USER/ADMIN/
    ROOT 不随 space 变），space 入索引会让「同 principal 同 role」变成两条互覆
    记录。本条钉住 principal 级语义。若业务需要 space 级 role，需先演进 §3.1。
    """
    actor = Scope(org="acme", space="s1", user="space-probe")
    key = store.issue(actor, Role.USER)

    # 网关声明的 actor 不带 space，仍能查到该 principal 的 role
    assert store.get_role(Scope(org="acme", user="space-probe")) is Role.USER
    store.revoke(fingerprint(key))


# -- 安全属性 ---------------------------------------------------------------- #


def test_registry_never_holds_plaintext(store) -> None:
    """最重要的回归防线：注册表里存的必须是哈希，不是明文。"""
    key = store.issue(Scope(org="acme", user="plaintext-check"), Role.USER)
    dumped = json.dumps(
        [
            {"fp": r.key_fp, "hash": r.key_hash, "org": r.actor.org, "revoked": r.revoked}
            for r in store._records.values()
        ]
    )
    assert key not in dumped
    assert dumped.count("$argon2id$") >= 1


def test_resolve_pads_time_on_miss(store) -> None:
    """未命中不得比「命中前缀但 key 错」快一整个 Argon2 verify。

    差异若存在是 ~100x 量级（差一整个 verify），故区间给到 [0.5, 2.0] 足以检出，
    同时容忍 CI 抖动。取中位数而非平均，避免单次 GC 抖动主导。
    """
    key = store.issue(Scope(org="acme", user="timing"), Role.USER)
    # 同前缀但内容不同 → 走「候选存在但 verify 失败」路径
    wrong_same_prefix = key[:8] + generate_api_key()[8:]

    def elapsed(candidate: str) -> float:
        start = time.perf_counter()
        store.resolve(candidate)
        return time.perf_counter() - start

    no_candidate = median(elapsed(generate_api_key()) for _ in range(5))
    wrong_key = median(elapsed(wrong_same_prefix) for _ in range(5))

    ratio = no_candidate / wrong_key
    assert 0.5 < ratio < 2.0, f"timing side channel: ratio={ratio:.2f}"


def test_single_resolve_stays_under_budget(store) -> None:
    """性能基线：防止 Argon2 参数被误配成更离谱的值。

    实测单次约 200ms（128 MiB × time_cost=4），对应 5~20 QPS/核——这是已知
    限制，不是本测试要防的；本测试只防「参数配错一个数量级」。
    """
    key = store.issue(Scope(org="acme", user="perf"), Role.USER)
    start = time.perf_counter()
    store.resolve(key)
    assert (time.perf_counter() - start) < 1.0
