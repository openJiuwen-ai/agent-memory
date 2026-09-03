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
