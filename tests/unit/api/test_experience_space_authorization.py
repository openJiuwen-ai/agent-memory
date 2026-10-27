"""经验写入的接口层行为（v3 mem2.0 §2.1）：缺省落点鉴权与生产者标记注入。

缺省落点鉴权（``WriteOps._authorize_experience_space``）：不带 ``coords`` 且
``default_space`` 填了空间名的经验写入，写入者对这个空间的写权限无处校验，须在
交给引擎之前按写入动作鉴权；``own`` 时落点就是写入落点，已由现有逻辑校验，此处
空过；未配置经验的部署不在此报错（引擎的 profile 准入会拒绝）。

生产者标记注入（``write_ops._with_producer_marks``）：经验写入的生产者主体与凭据
指纹由接口层从认证结果写进两个内核保留键，调用方自报的同名键被覆盖——补报核对
与外部结果阈值都按生产者取值，不能让请求冒用其他接入方。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.api.memory_api_impl.write_ops import _with_producer_marks
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import AuthContext, Surface
from jiuwen_memory.common.type_def import (
    PRODUCER_CREDENTIAL_KEY,
    PRODUCER_PRINCIPAL_KEY,
    Scope,
)
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import Action

pytestmark = pytest.mark.unit

_ACTOR = Scope(org="t", user="u")


def _sec(*, actor: Scope = _ACTOR):
    """鉴权入参形态：``_authorize_experience_space`` 收 ``RequestSecurityContext``
    （内部取 ``security.auth.actor`` 构造目标空间——与 ``_authorize`` 的第一参数
    契约一致，单测此前传 ``Scope`` 与真实契约脱节，e2e 真装配下当场 AttributeError）。"""
    return new_request_context(
        AuthContext(actor=actor, credential_id=""),
        surface=Surface.HTTP,
    )


@pytest.fixture
def api():
    return assemble()


def _call_authorize_experience_space(api, security, coords, payload) -> None:
    """白盒触达缺省落点鉴权（接口层内部步骤，经小工具绕行受保护访问）。"""
    getattr(api, "_authorize_experience_space")(security, coords, payload)


def _experience_default_space(api) -> str:
    """白盒读取装配后的经验缺省落点配置。"""
    return getattr(api, "_experience_default_space")


def _experience_deleted_callbacks(api) -> list:
    """白盒读取按作者清除的删除事务回调清单。"""
    return getattr(getattr(api, "_space_lifecycle"), "_on_deleted")


class _AuthorizeRecorder:
    """替身鉴权：记录调用，可注入拒绝。"""

    def __init__(self, *, deny: bool = False) -> None:
        self.calls: list[tuple[Scope, Scope, Action, str]] = []
        self._deny = deny

    def __call__(self, identity, target, action, entry, *args, **kwargs):
        self.calls.append((identity, target, action, entry))
        if self._deny:
            raise PermissionDeniedError(action.value)
        return {}


def _api_with_default_space(monkeypatch, default_space, *, deny: bool = False):
    api = assemble()
    recorder = _AuthorizeRecorder(deny=deny)
    monkeypatch.setattr(api, "_experience_default_space", default_space)
    monkeypatch.setattr(api, "_authorize", recorder)
    return api, recorder


def test_non_experience_memory_type_skips(api, monkeypatch) -> None:
    """非经验类别的写入不经此鉴权（落点由正常链路裁决）。"""
    recorder = _AuthorizeRecorder()
    monkeypatch.setattr(api, "_authorize", recorder)

    _call_authorize_experience_space(api, _sec(), None, {"memory_type": "chat"})

    assert recorder.calls == [], "非经验类别不触发缺省落点鉴权"


def test_write_with_coords_skips(api, monkeypatch) -> None:
    """带 coords 的写入由落点判定计算候选空间时校验，不在此重复鉴权。"""
    recorder = _AuthorizeRecorder()
    monkeypatch.setattr(api, "_authorize", recorder)

    _call_authorize_experience_space(
        api, _sec(), {"project_id": "p1"}, {"memory_type": "experience"}
    )

    assert recorder.calls == [], "带 coords 的写入走落点判定，不经此鉴权"


def test_default_space_own_skips(monkeypatch) -> None:
    """``own``：落点就是写入落点，写权已由现有逻辑校验，此处空过。"""
    api, recorder = _api_with_default_space(monkeypatch, "own")

    _call_authorize_experience_space(
        api, _sec(), None, {"memory_type": "experience"}
    )

    assert recorder.calls == [], "own 时不做缺省落点鉴权"


def test_default_space_name_authorized_for_write(monkeypatch) -> None:
    """``default_space`` 填空间名：对它按写入动作鉴权，目标取主体同组织。"""
    api, recorder = _api_with_default_space(monkeypatch, "shared-experience")

    _call_authorize_experience_space(
        api, _sec(), None, {"memory_type": "experience"}
    )

    assert len(recorder.calls) == 1, "对缺省落点空间按写入动作鉴权一次"
    security_arg, target, action, entry = recorder.calls[0]
    assert security_arg.auth.actor == _ACTOR, "鉴权入参是完整安全上下文（非裸 actor）"
    assert target == Scope(org="t", space="shared-experience"), "目标取主体同组织的缺省落点"
    assert (action, entry) == (Action.WRITE, "add"), "按写入动作鉴权"


def test_write_denial_propagates(monkeypatch) -> None:
    """缺省落点写权被拒：照常拒绝（接入方身份须对该空间有写权限）。"""
    api, _ = _api_with_default_space(monkeypatch, "shared-experience", deny=True)

    with pytest.raises(PermissionDeniedError):
        _call_authorize_experience_space(
            api, _sec(), None, {"memory_type": "experience"}
        )


def test_legacy_actor_scope_keeps_org(monkeypatch) -> None:
    """目标空间取主体同组织：org 一致、space 换成缺省落点名、主体维不带。"""
    actor = Scope(org="acme", user="alice")
    api, recorder = _api_with_default_space(monkeypatch, "exp-space")

    _call_authorize_experience_space(
        api, _sec(actor=actor), None, {"memory_type": " experience "}
    )

    assert recorder.calls[0][1] == Scope(org="acme", space="exp-space"), (
        "目标只带 org 与 space 两维"
    )


# -- globals.experience 配置解析（v3 mem2.0 §2 部署配置）---------------------- #


def test_experience_globals_reach_api() -> None:
    """``globals.experience`` 的 ``default_space`` 传到 API 的缺省落点鉴权。"""
    api = assemble(
        config=Config.from_dict(
            {
                "globals": {
                    "experience": {
                        "memory_class": "project",
                        "default_space": "exp-shared",
                        "curated_spaces": [
                            {"space": "exp-shared", "skill": True, "repro_unit": "user"}
                        ],
                    }
                }
            }
        )
    )

    assert _experience_default_space(api) == "exp-shared", "default_space 原样到达"


def test_experience_absent_defaults_to_own() -> None:
    """未配置经验的部署取缺省 ``own``（鉴权跳过，由 profile 准入兜底）。"""
    api = assemble()

    assert _experience_default_space(api) == "own", "缺省 own"


def test_experience_default_space_own_explicit() -> None:
    """显式配置 ``default_space: own`` 与缺省等价。"""
    api = assemble(
        config=Config.from_dict(
            {"globals": {"experience": {"default_space": "own"}}}
        )
    )

    assert _experience_default_space(api) == "own", "显式 own"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("exp-a", "must be a mapping"),
        ({"curated_spaces": "exp-a"}, "must be a list"),
        ({"curated_spaces": ["exp-a"]}, "每一项须是映射"),
        ({"curated_spaces": [{"skill": True}]}, "须声明 space"),
        ({"curated_spaces": [{"space": "  "}]}, "须声明 space"),
        ({"curated_spaces": [{"space": "a"}, {"space": "a"}]}, "空间名重复"),
        (
            {"curated_spaces": [{"space": "a", "skill_release": "fast"}]},
            "skill_release",
        ),
        (
            {"curated_spaces": [{"space": "a", "repro_unit": "event"}]},
            "repro_unit",
        ),
    ],
)
def test_invalid_experience_globals_rejected(raw: object, message: str) -> None:
    """非法形态在装配期报参数错误，不等到写入时才失败。"""
    with pytest.raises(ValidationError, match=message):
        assemble(config=Config.from_dict({"globals": {"experience": raw}}))


# -- 按作者清除的登记回调（v3 mem2.0 §2.5.2「按作者清除」的装配） ------------- #


def test_erasure_callback_injected_from_appraiser_params() -> None:
    """author_key 配在 appraiser 实例参数（评价算子同款路径）时注入回调。"""
    api = assemble(
        config=Config.from_dict(
            {
                "appraiser": {
                    "default": {
                        "target": "experience",
                        "params": {"author_key": "k1"},
                    }
                },
                "globals": {
                    "experience": {
                        "curated_spaces": [{"space": "exp-a"}],
                    }
                },
            }
        )
    )

    callbacks = _experience_deleted_callbacks(api)
    assert len(callbacks) == 1, "周期整理空间与 author_key 都已配置：回调注入删除事务"
    # 直接触发不抛：闭包签名与 UtilityStore 构造无误（写进默认 KV）。
    callbacks[0]("t", "personal", [Scope(org="t", user="u1")])


def test_erasure_callback_injected_from_globals_author_key() -> None:
    """author_key 写在 globals 层（回退路径）同样注入。"""
    api = assemble(
        config=Config.from_dict(
            {
                "globals": {
                    "author_key": "k2",
                    "experience": {"curated_spaces": [{"space": "exp-a"}]},
                }
            }
        )
    )

    assert len(_experience_deleted_callbacks(api)) == 1, "globals 的 author_key 也可用"


def test_erasure_callback_absent_without_author_key() -> None:
    """author_key 缺失时特性关闭：删除事务保持缺省（无回调、现有行为）。"""
    api = assemble(
        config=Config.from_dict(
            {"globals": {"experience": {"curated_spaces": [{"space": "exp-a"}]}}}
        )
    )

    assert _experience_deleted_callbacks(api) == [], "无 author_key 不注入回调"


def test_erasure_callback_absent_without_curated_spaces() -> None:
    """未配置周期整理空间时特性关闭（没有空间可写清除标记）。"""
    api = assemble(
        config=Config.from_dict(
            {"globals": {"author_key": "k", "experience": {"default_space": "own"}}}
        )
    )

    assert _experience_deleted_callbacks(api) == [], "无 curated_spaces 不注入回调"


def test_erasure_store_affinity_violation_rejected() -> None:
    """curator/appraiser 的 message_store 指向别的 KV 实例：装配期拒绝。

    标记写进算子读不到的存储时，注销后的合规清除会静默失效——按作者清除
    启用（curated_spaces + author_key）即校验，坏配置在启动期暴露。
    """
    base_globals = {
        "author_key": "k1",
        "experience": {"curated_spaces": [{"space": "exp-a"}]},
    }

    for top_name in ("appraiser", "curator"):
        config = Config.from_dict(
            {
                "globals": base_globals,
                "kv_store": {"default": "memory", "other": "memory"},
                top_name: {
                    "default": {
                        "target": "experience",
                        "params": {"message_store": "other"},
                    }
                },
            }
        )
        with pytest.raises(ValidationError, match="message_store"):
            assemble(config=config)


def test_erasure_store_affinity_same_reference_passes() -> None:
    """显式声明 message_store 指向 default：同一实例，照常注入回调。"""
    api = assemble(
        config=Config.from_dict(
            {
                "globals": {
                    "author_key": "k1",
                    "experience": {"curated_spaces": [{"space": "exp-a"}]},
                },
                "kv_store": {"default": "memory"},
                "curator": {
                    "default": {
                        "target": "experience",
                        "params": {"message_store": "default"},
                    }
                },
            }
        )
    )

    assert len(_experience_deleted_callbacks(api)) == 1, "同源引用不影响回调注入"


def test_erasure_author_key_conflict_rejected() -> None:
    """appraiser 多实例 author_key 不一致：装配期拒绝（摘要错位即清除失效）。"""
    config = Config.from_dict(
        {
            "globals": {
                "author_key": "k1",
                "experience": {"curated_spaces": [{"space": "exp-a"}]},
            },
            "appraiser": {
                "a1": {
                    "target": "experience",
                    "params": {"author_key": "k1"},
                },
                "a2": {
                    "target": "experience",
                    "params": {"author_key": "k2"},
                },
            },
        }
    )

    with pytest.raises(ValidationError, match="author_key"):
        assemble(config=config)


# -- 生产者标记注入（v3 mem2.0 §2.1：认证结果写进内核保留键）------------------ #


def _security(*, actor: Scope, credential_id: str = ""):
    """构造带指定认证结果的请求安全上下文（受控入口 ``new_request_context``）。"""
    return new_request_context(
        AuthContext(actor=actor, credential_id=credential_id),
        surface=Surface.HTTP,
    )


def test_producer_marks_injected_for_experience_write() -> None:
    """经验写入：认证结果写进两个保留键（主体取 agent 维、凭据取指纹）。"""
    security = _security(
        actor=Scope(org="t", agent="a1"), credential_id="cred-9"
    )

    marked = _with_producer_marks({"memory_type": "experience"}, security)

    assert marked[PRODUCER_PRINCIPAL_KEY] == "agent:a1", "生产者主体取认证 actor"
    assert marked[PRODUCER_CREDENTIAL_KEY] == "cred-9", "凭据指纹取认证结果"
    assert marked["memory_type"] == "experience", "原 metadata 键保留"


def test_producer_marks_derive_user_principal() -> None:
    """用户主体认证：生产者主体推导为 ``user:<id>``。"""
    security = _security(actor=Scope(org="t", user="alice"), credential_id="c1")

    marked = _with_producer_marks({"memory_type": " experience "}, security)

    assert marked[PRODUCER_PRINCIPAL_KEY] == "user:alice", (
        "memory_type 去空白后命中经验类别，主体取 user 维"
    )


def test_producer_marks_skip_non_experience() -> None:
    """非经验写入不带生产者标记（两个保留键不出现）。"""
    security = _security(actor=Scope(org="t", agent="a1"), credential_id="c1")

    chat = _with_producer_marks({"memory_type": "chat"}, security)
    none_meta = _with_producer_marks(None, security)

    assert PRODUCER_PRINCIPAL_KEY not in chat and PRODUCER_CREDENTIAL_KEY not in chat, (
        "非经验类别不注入保留键"
    )
    assert none_meta is None, "无 metadata 的写入原样返回"


def test_producer_marks_skip_empty_actor() -> None:
    """主体维全空的调用方（运维通道）跳过注入：评价算子按缺少生产者标记拒绝。

    受控入口 ``new_request_context`` 按单主体不变量拒绝 user/agent 全空的 actor，
    此分支是纯函数对绕过受控入口的历史/内部路径的防御——经 ``replace`` 直接构造。
    """
    base = _security(actor=Scope(org="t", agent="a1"))
    security = replace(base, auth=replace(base.auth, actor=Scope(org="t")))

    marked = _with_producer_marks({"memory_type": "experience"}, security)

    assert PRODUCER_PRINCIPAL_KEY not in marked, "主体维全空不注入生产者标记"


def test_producer_marks_written_with_empty_credential() -> None:
    """凭据指纹为空串也写键（认证结果未携带指纹时不省略键，保持形态稳定）。"""
    security = _security(actor=Scope(org="t", agent="a1"))

    marked = _with_producer_marks({"memory_type": "experience"}, security)

    assert marked[PRODUCER_CREDENTIAL_KEY] == "", "空串恒写入不省略键"


def test_producer_marks_override_caller_supplied() -> None:
    """调用方自报的同名键被认证结果覆盖（写入边界先拒占用，此处是纵深防御）。"""
    security = _security(actor=Scope(org="t", agent="a1"), credential_id="real")
    metadata = {
        "memory_type": "experience",
        PRODUCER_PRINCIPAL_KEY: "user:attacker",
        PRODUCER_CREDENTIAL_KEY: "forged",
    }

    marked = _with_producer_marks(metadata, security)

    assert marked[PRODUCER_PRINCIPAL_KEY] == "agent:a1", "自报主体被认证主体覆盖"
    assert marked[PRODUCER_CREDENTIAL_KEY] == "real", "自报指纹被认证指纹覆盖"
