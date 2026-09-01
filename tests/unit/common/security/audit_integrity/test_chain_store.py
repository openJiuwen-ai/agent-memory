# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""jiuwen_memory.common.security.audit_integrity.chain_store: 契约层--capability 与链结构。

接口先行版：内存 / SQLite 后端叠加实现随实装 PR 合入，只固定
``ChainedAuditStore`` / ``AuditAnchor`` 的契约形状与 ``GENESIS_DIGEST`` 常量。
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from jiuwen_memory.common.security.audit_integrity.base import Proof
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    GENESIS_DIGEST,
    AnchorRecord,
    AuditAnchor,
    ChainedAuditStore,
    ChainedRecord,
    ChainHead,
    ChainSnapshot,
    ChainStoreCapability,
)
from jiuwen_memory.common.type_def import AuditEvent

pytestmark = pytest.mark.unit


def test_genesis_digest_is_64_zero_hex() -> None:
    """genesis 前序摘要固定为 SHA-256 长度的全零 hex，与真实 HMAC 输出可区分。"""
    assert GENESIS_DIGEST == "0" * 64


def test_capability_declares_exactly_six_booleans() -> None:
    """六项行为保证齐全即通过构造；缺任一项（构造失败）即拒绝装配。"""
    capability = ChainStoreCapability(
        persistent=True,
        atomic_append=True,
        stable_head_snapshot=True,
        key_epoch=True,
        external_anchor=False,
        streaming_scan=True,
    )
    assert [f.name for f in dataclasses.fields(capability)] == [
        "persistent",
        "atomic_append",
        "stable_head_snapshot",
        "key_epoch",
        "external_anchor",
        "streaming_scan",
    ]
    with pytest.raises(TypeError):
        ChainStoreCapability(persistent=True)  # type: ignore[call-arg]

    with pytest.raises(TypeError, match="persistent"):
        ChainStoreCapability(
            persistent=1,  # type: ignore[arg-type]
            atomic_append=True,
            stable_head_snapshot=True,
            key_epoch=True,
            external_anchor=False,
            streaming_scan=True,
        )


def test_chain_store_cannot_be_partially_implemented() -> None:
    class Incomplete(ChainedAuditStore):
        def capabilities(self) -> ChainStoreCapability:
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_anchor_cannot_be_partially_implemented() -> None:
    class Incomplete(AuditAnchor):
        def anchor_head(self, head, *, chain_id):
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_chain_value_objects_are_frozen() -> None:
    """链头 / 记录 / 快照 / 锚点记录均不可变：链式证明的游标不允许事后篡改。"""
    head = ChainHead(sequence=1, digest="a" * 64, key_id="kp-1", key_epoch=1, format_version=1)
    record = ChainedRecord(
        event=AuditEvent(action="write", decision="allow"),
        proof=Proof(
            format_version=1,
            sequence=1,
            previous_digest=GENESIS_DIGEST,
            digest="a" * 64,
            key_id="kp-1",
            key_epoch=1,
        ),
    )
    snapshot = ChainSnapshot(
        head=head,
        last_record=record,
        after_sequence=1,
        checkpoint=record,
    )
    anchored = AnchorRecord(
        chain_id="chain-1",
        sequence=1,
        digest="a" * 64,
        key_id="kp-1",
        epoch=1,
        format_version=1,
        anchored_at="2026-08-21T00:00:00Z",
    )
    for obj, attr in (
        (head, "digest"),
        (record.proof, "digest"),
        (snapshot, "head"),
        (anchored, "digest"),
    ):
        with pytest.raises(AttributeError):
            setattr(obj, attr, "b" * 64)  # type: ignore[misc]


def test_empty_chain_snapshot_allows_missing_last_record() -> None:
    """空链快照：sequence=0 的 genesis head + last_record=None。"""
    head = ChainHead(sequence=0, digest=GENESIS_DIGEST, key_id="", key_epoch=0, format_version=1)
    snapshot = ChainSnapshot(head=head, last_record=None)
    assert snapshot.last_record is None
    assert snapshot.head.digest == GENESIS_DIGEST


def test_incremental_snapshot_requires_an_exact_checkpoint_sequence() -> None:
    head = ChainHead(sequence=7, digest="b" * 64, key_id="kp-1", key_epoch=1, format_version=1)
    checkpoint = ChainedRecord(
        event=AuditEvent(action="write", decision="allow"),
        proof=Proof(
            format_version=1,
            sequence=6,
            previous_digest="a" * 64,
            digest="b" * 64,
            key_id="kp-1",
            key_epoch=1,
        ),
    )

    with pytest.raises(ValueError, match="checkpoint sequence"):
        ChainSnapshot(
            head=head,
            last_record=None,
            after_sequence=7,
            checkpoint=checkpoint,
        )


def test_incremental_scan_contract_freezes_checkpoint_and_upper_bound() -> None:
    snapshot_params = inspect.signature(ChainedAuditStore.read_stable_snapshot).parameters
    assert list(snapshot_params) == ["self", "after_sequence"]
    assert snapshot_params["after_sequence"].default == 0

    scan_params = inspect.signature(ChainedAuditStore.scan).parameters
    assert list(scan_params) == ["self", "after_sequence", "limit", "through_sequence"]
    assert scan_params["through_sequence"].kind is inspect.Parameter.KEYWORD_ONLY
