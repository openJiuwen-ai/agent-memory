# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""jiuwen_memory.common.security.audit_integrity.base: 契约层--状态、值对象与序列化。

接口先行版：``audit_integrity_impl`` 未合入，只固定
:class:`~common.security.audit_integrity.base.AuditIntegrityProvider` 的契约形状
与 ``AuditVerificationResult.to_body`` 的对外 wire 格式（PR3 接口文档 §6.1）。
"""

from __future__ import annotations

import inspect

import pytest

from jiuwen_memory.common.errors import AgentMemoryError
from jiuwen_memory.common.security.audit_integrity.base import (
    HARD_MAX_AUDIT_VERIFY_PAGE_SIZE,
    HARD_MAX_AUDIT_VERIFY_SAMPLES,
    AnchorState,
    AnchorStatus,
    AuditIntegrityError,
    AuditIntegrityProducer,
    AuditIntegrityProvider,
    AuditIntegrityStatus,
    AuditMigrationRequiredError,
    AuditSchemaError,
    AuditVerificationLimits,
    AuditVerificationResult,
    ChainConflictError,
    KeyCapabilityError,
    Proof,
)
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    ChainStoreCapability,
)
from jiuwen_memory.common.type_def import AuditEvent

pytestmark = pytest.mark.unit


def test_error_taxonomy_is_single_rooted() -> None:
    """四个审计完整性异常同根于 AuditIntegrityError，并归属 AgentMemoryError 语义域。"""
    for exc in (
        AuditMigrationRequiredError,
        ChainConflictError,
        AuditSchemaError,
        KeyCapabilityError,
    ):
        assert issubclass(exc, AuditIntegrityError)
    assert issubclass(AuditIntegrityError, AgentMemoryError)


def test_status_values_are_wire_stable() -> None:
    """状态值是线上契约（§6.1 Body 的 status 字段），五个取值一经发布不得漂移。"""
    assert AuditIntegrityStatus.UNSUPPORTED.value == "unsupported"
    assert AuditIntegrityStatus.CLEAN.value == "clean"
    assert AuditIntegrityStatus.TAMPERED.value == "tampered"
    assert AuditIntegrityStatus.INCOMPLETE.value == "incomplete"
    assert AuditIntegrityStatus.ROLLBACK_SUSPECTED.value == "rollback_suspected"


def test_anchor_status_values_are_wire_stable_and_cross_fields_are_consistent() -> None:
    assert [status.value for status in AnchorStatus] == [
        "",
        "ok",
        "lagging",
        "conflict",
        "unavailable",
    ]
    assert AnchorState(checked=False).status is AnchorStatus.UNCHECKED
    assert AnchorState(checked=True, status=AnchorStatus.OK).status is AnchorStatus.OK

    with pytest.raises(TypeError, match="AnchorStatus"):
        AnchorState(checked=True, status="ok")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unchecked anchor"):
        AnchorState(checked=False, status=AnchorStatus.OK)
    with pytest.raises(ValueError, match="checked anchor"):
        AnchorState(checked=True)


def test_provider_cannot_be_partially_implemented() -> None:
    class Incomplete(AuditIntegrityProvider):
        def capabilities(self) -> ChainStoreCapability:
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_producer_declares_top_name() -> None:
    assert AuditIntegrityProducer.TOP_NAME == "audit_integrity"


def test_proof_is_frozen() -> None:
    """证明字段（摘要、key 代次）不允许事后篡改。"""
    proof = Proof(
        format_version=1,
        sequence=3,
        previous_digest="0" * 64,
        digest="a" * 64,
        key_id="kp-1",
        key_epoch=2,
    )
    with pytest.raises(AttributeError):
        proof.digest = "b" * 64  # type: ignore[misc]


def test_proof_rejects_wrong_runtime_field_types() -> None:
    with pytest.raises(TypeError, match="sequence"):
        Proof(
            format_version=1,
            sequence="3",  # type: ignore[arg-type]
            previous_digest="0" * 64,
            digest="a" * 64,
            key_id="kp-1",
            key_epoch=2,
        )


def test_result_to_body_is_pure_dict_wire_contract() -> None:
    """to_body 输出 §6.1 确认的纯 dict：无嵌套对象、samples 展开为 dict、无 ok 字段。"""
    proof = Proof(
        format_version=1,
        sequence=7,
        previous_digest="0" * 64,
        digest="a" * 64,
        key_id="kp-1",
        key_epoch=2,
    )
    result = AuditVerificationResult(
        status=AuditIntegrityStatus.TAMPERED,
        checked_count=10,
        error_count=1,
        truncated=True,
        high_water_mark=9,
        key_epoch_range=(1, 3),
        anchor=AnchorState(
            checked=True,
            status=AnchorStatus.CONFLICT,
            detail="anchor mismatch",
        ),
        samples=(proof,),
        detail="2 records diverge",
    )

    body = result.to_body()

    assert body == {
        "op": "verify_audit",
        "status": "tampered",
        "checked_count": 10,
        "error_count": 1,
        "truncated": True,
        "high_water_mark": 9,
        "key_epoch_range": [1, 3],
        "anchor": {"checked": True, "status": "conflict", "detail": "anchor mismatch"},
        "samples": [
            {
                "sequence": 7,
                "format_version": 1,
                "previous_digest": "0" * 64,
                "digest": "a" * 64,
                "key_id": "kp-1",
                "key_epoch": 2,
            }
        ],
        "detail": "2 records diverge",
    }
    # 严格按文档：Body 不含 ok / evidence / secret 类字段
    assert "ok" not in body


def test_result_to_body_defaults_are_serializable() -> None:
    """空结果（unsupported/未验证）也能序列化：samples 空、epoch 区间为 [0, 0]。"""
    body = AuditVerificationResult(
        status=AuditIntegrityStatus.UNSUPPORTED,
        checked_count=0,
        error_count=0,
        truncated=False,
        high_water_mark=0,
        key_epoch_range=(0, 0),
        anchor=AnchorState(checked=False),
    ).to_body()

    assert body["samples"] == []
    assert body["anchor"] == {"checked": False, "status": "", "detail": ""}


def test_result_freezes_iterables_and_rejects_wrong_runtime_types() -> None:
    proof = Proof(
        format_version=1,
        sequence=1,
        previous_digest="0" * 64,
        digest="a" * 64,
        key_id="kp-1",
        key_epoch=1,
    )
    result = AuditVerificationResult(
        status=AuditIntegrityStatus.CLEAN,
        checked_count=1,
        error_count=0,
        truncated=False,
        high_water_mark=1,
        key_epoch_range=[1, 1],  # type: ignore[arg-type]
        anchor=AnchorState(checked=False),
        samples=[proof],  # type: ignore[arg-type]
    )

    assert result.key_epoch_range == (1, 1)
    assert result.samples == (proof,)
    with pytest.raises(TypeError, match="status"):
        AuditVerificationResult(
            status="clean",  # type: ignore[arg-type]
            checked_count=1,
            error_count=0,
            truncated=False,
            high_water_mark=1,
            key_epoch_range=(1, 1),
            anchor=AnchorState(checked=False),
        )


def test_server_limits_reject_unbounded_configuration() -> None:
    limits = AuditVerificationLimits(max_page_size=50, max_samples=2)
    assert limits.max_page_size == 50
    assert limits.max_samples == 2

    with pytest.raises(ValueError, match="hard limit"):
        AuditVerificationLimits(max_page_size=HARD_MAX_AUDIT_VERIFY_PAGE_SIZE + 1)
    with pytest.raises(ValueError, match="hard limit"):
        AuditVerificationLimits(max_samples=HARD_MAX_AUDIT_VERIFY_SAMPLES + 1)
    assert AuditVerificationLimits(max_samples=0).max_samples == 0


def test_result_rejects_samples_above_absolute_server_limit() -> None:
    proof = Proof(
        format_version=1,
        sequence=1,
        previous_digest="0" * 64,
        digest="a" * 64,
        key_id="kp-1",
        key_epoch=1,
    )
    with pytest.raises(ValueError, match="absolute server-side limit"):
        AuditVerificationResult(
            status=AuditIntegrityStatus.TAMPERED,
            checked_count=HARD_MAX_AUDIT_VERIFY_SAMPLES + 1,
            error_count=HARD_MAX_AUDIT_VERIFY_SAMPLES + 1,
            truncated=True,
            high_water_mark=HARD_MAX_AUDIT_VERIFY_SAMPLES + 1,
            key_epoch_range=(1, 1),
            anchor=AnchorState(checked=False),
            samples=(proof,) * (HARD_MAX_AUDIT_VERIFY_SAMPLES + 1),
        )


def test_verify_signature_only_accepts_server_side_params() -> None:
    """verify 只接受服务端验证参数，不得出现调用方可控的 digest/key/proof 入参。"""
    params = inspect.signature(AuditIntegrityProvider.verify).parameters
    assert list(params) == ["self", "after_sequence", "page_size", "max_samples", "anchor_policy"]
    for name in ("after_sequence", "page_size", "max_samples", "anchor_policy"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_is_test_only_defaults_false() -> None:
    """默认非测试实现：生产 Runtime 不拒绝。"""

    class _Provider(AuditIntegrityProvider):
        def capabilities(self) -> ChainStoreCapability:
            raise NotImplementedError

        def chain_store(self):
            raise NotImplementedError

        def record_chained(self, event: AuditEvent):
            raise NotImplementedError

        def verify(self, **kwargs) -> AuditVerificationResult:
            raise NotImplementedError

        def active_key_ref(self):
            raise NotImplementedError

        def health(self) -> None:
            return None

    assert _Provider().is_test_only() is False
