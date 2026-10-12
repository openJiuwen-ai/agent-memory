from __future__ import annotations

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import UnsupportedCapabilityError
from jiuwen_memory.common.normalizer import Normalizer
from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
    PassthroughNormalizer,
)
from jiuwen_memory.common.type_def import Modality, RawPayload, Scope
from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor

pytestmark = pytest.mark.unit


class _ImageNormalizer(Normalizer):
    def __init__(self) -> None:
        self.payloads: list[RawPayload] = []

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.NORMALIZER

    @staticmethod
    def health() -> None:
        return None

    @staticmethod
    def modalities() -> list[Modality]:
        return [Modality.IMAGE]

    def normalize(self, payload: RawPayload) -> str:
        self.payloads.append(payload)
        return "recognized image"


def test_simple_ingestor_copies_payload_assets_to_its_segment() -> None:
    assets = ["file:///video.mp4", "file:///transcript.json"]
    payload = RawPayload(
        id="payload-1",
        scope=Scope(org="acme", user="alice"),
        modality=Modality.TEXT,
        data=b"normalized content",
        assets=assets,
    )

    units = SimpleIngestor(PassthroughNormalizer()).ingest([payload])

    assert len(units) == 1
    assert len(units[0].segments) == 1
    assert units[0].segments[0].assets == assets
    assert units[0].segments[0].assets is not payload.assets


def test_simple_ingestor_rejects_unsupported_modality_before_normalize() -> None:
    payload = RawPayload(
        id="image-1",
        modality=Modality.IMAGE,
        uri="file:///photo.jpg",
    )

    with pytest.raises(UnsupportedCapabilityError) as error:
        SimpleIngestor(PassthroughNormalizer()).ingest([payload])

    assert error.value.capability == "modality"
    assert error.value.value == "image"
    assert error.value.component == "PassthroughNormalizer"


def test_simple_ingestor_accepts_custom_normalizer_declared_modality() -> None:
    normalizer = _ImageNormalizer()
    payload = RawPayload(
        id="image-1",
        modality=Modality.IMAGE,
        uri="file:///photo.jpg",
    )

    units = SimpleIngestor(normalizer).ingest([payload])

    assert normalizer.payloads == [payload]
    assert units[0].content == "recognized image"


# ---------------------------------------------------------------------------
# 超大文本预切分（BUG2026092802777）
# ---------------------------------------------------------------------------


def _text_payload(text: str) -> RawPayload:
    return RawPayload(
        id="payload-split",
        scope=Scope(org="acme", user="alice"),
        modality=Modality.TEXT,
        data=text.encode("utf-8"),
    )


def test_simple_ingestor_short_content_single_unit() -> None:
    """未超限内容保持单 unit，不写 _part/_parts。"""
    units = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=100).ingest(
        [_text_payload("short content")]
    )

    assert len(units) == 1
    assert units[0].content == "short content"
    assert "_part" not in units[0].system_metadata
    assert "_parts" not in units[0].system_metadata


def test_simple_ingestor_splits_long_content_by_paragraph() -> None:
    """多段落超限文本按段落聚合切分：每片 ≤ 上限，可逆拼接，_part/_parts 正确。"""
    paras = [f"para{i} " + "x" * 40 for i in range(10)]  # 每段 46 chars
    text = "\n\n".join(paras)  # 478 chars
    units = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=200).ingest(
        [_text_payload(text)]
    )

    assert len(units) > 1
    assert all(len(u.content) <= 200 for u in units)
    assert "\n\n".join(u.content for u in units) == text  # 无损可逆
    assert units[0].system_metadata["_part"] == 1
    assert units[0].system_metadata["_parts"] == len(units)
    assert units[-1].system_metadata["_part"] == len(units)
    assert all(u.source_ref == "payload-split" for u in units)


def test_simple_ingestor_hard_cuts_oversized_single_paragraph() -> None:
    """无段落边界的超长单段硬切兜底：切片拼接可还原原文。"""
    text = "y" * 500
    units = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=200).ingest(
        [_text_payload(text)]
    )

    assert len(units) == 3
    assert all(len(u.content) <= 200 for u in units)
    assert "".join(u.content for u in units) == text
    assert units[0].system_metadata["_parts"] == 3


def test_simple_ingestor_split_disabled_with_non_positive_limit() -> None:
    """max_unit_chars <= 0 关闭预切分，保持旧行为。"""
    text = "z" * 500
    units = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=0).ingest(
        [_text_payload(text)]
    )

    assert len(units) == 1
    assert units[0].content == text
