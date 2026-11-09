from __future__ import annotations

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError
from jiuwen_memory.common.normalizer import ensure_structured_modalities_declared
from jiuwen_memory.common.normalizer.base import Normalizer
from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
    PassthroughNormalizer,
)
from jiuwen_memory.common.normalizer.normalizer_impl.routing_normalizer import (
    RoutingNormalizer,
)
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.type_def import Modality, RawPayload

pytestmark = pytest.mark.unit


def test_passthrough_rejects_unsupported_media_uri() -> None:
    payload = RawPayload(modality=Modality.IMAGE, uri="file:///photo.jpg")

    with pytest.raises(UnsupportedCapabilityError, match="modality 'image'"):
        PassthroughNormalizer().normalize(payload)


def test_passthrough_keeps_supported_text_uri_fallback() -> None:
    payload = RawPayload(modality=Modality.TEXT, uri="file:///note.txt")

    assert PassthroughNormalizer().normalize(payload) == "file:///note.txt"


def test_passthrough_accepts_utf8_code_text() -> None:
    payload = RawPayload(modality=Modality.CODE, data=b"def run(): pass")

    assert PassthroughNormalizer().normalize(payload) == "def run(): pass"


def test_passthrough_rejects_document_without_parser() -> None:
    payload = RawPayload(modality=Modality.DOCUMENT, data=b"plain content")

    with pytest.raises(UnsupportedCapabilityError, match="modality 'document'"):
        PassthroughNormalizer().normalize(payload)


def test_passthrough_rejects_code_uri_fallback() -> None:
    payload = RawPayload(modality=Modality.CODE, uri="file:///repo/main.py")

    with pytest.raises(ValidationError, match="URI fallback for TEXT"):
        PassthroughNormalizer().normalize(payload)


def test_passthrough_reports_non_utf8_text_as_validation_error() -> None:
    payload = RawPayload(modality=Modality.CODE, data=b"\xff")

    with pytest.raises(ValidationError, match="requires UTF-8 text data"):
        PassthroughNormalizer().normalize(payload)


def test_routing_rejects_incompatible_static_route() -> None:
    with pytest.raises(ValidationError, match="route 'video'.*PassthroughNormalizer"):
        RoutingNormalizer(
            PassthroughNormalizer(),
            {Modality.VIDEO: PassthroughNormalizer()},
        )


def test_video_normalizer_reports_unsupported_modality_consistently() -> None:
    payload = RawPayload(modality=Modality.TEXT, data=b"plain text")

    with pytest.raises(UnsupportedCapabilityError) as error:
        VideoNormalizer(backend=lambda _payload: ([], [])).normalize(payload)

    assert error.value.value == "text"
    assert error.value.component == "VideoNormalizer"


def test_structured_modalities_default_empty_and_video_declares_itself() -> None:
    """缺省不声明结构化产物；视频规约器声明 VIDEO（其包络须整体交付抽取器，issue #294）。"""
    assert PassthroughNormalizer().structured_modalities() == frozenset(), (
        "passthrough 的文本投影不是结构化产物，应走缺省空集"
    )
    assert VideoNormalizer(backend=lambda payload: ([], [])).structured_modalities() == frozenset(
        {Modality.VIDEO}
    ), "视频规约器必须声明 VIDEO 为结构化产物"


def test_routing_structured_modalities_follow_dispatch() -> None:
    """RoutingNormalizer 的声明与 normalize 分派一致：route 看 delegate，其余看 fallback。"""
    video = VideoNormalizer(backend=lambda payload: ([], []))
    routing = RoutingNormalizer(fallback=PassthroughNormalizer(), routes={Modality.VIDEO: video})

    assert routing.structured_modalities() == frozenset({Modality.VIDEO}), (
        "显式 route 到视频规约器的 VIDEO 应被汇总为结构化产物"
    )

    text_only = RoutingNormalizer(fallback=PassthroughNormalizer(), routes={})
    assert text_only.structured_modalities() == frozenset(), "无结构化 delegate 时应为空集"


def test_structured_modalities_must_be_subset_of_modalities() -> None:
    """声明了不支持的模态为结构化产物属于实现错误，装配期拒绝。"""

    class _MisdeclaredNormalizer(Normalizer):
        @staticmethod
        def plugin_type() -> PluginType:
            return PluginType.NORMALIZER

        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def modalities() -> list[Modality]:
            return [Modality.TEXT]

        def normalize(self, payload: RawPayload) -> str:
            del payload
            return ""

        @staticmethod
        def structured_modalities() -> frozenset[Modality]:
            return frozenset({Modality.VIDEO})

    with pytest.raises(ValidationError, match="structured_modalities.*video"):
        ensure_structured_modalities_declared(_MisdeclaredNormalizer())

    assert ensure_structured_modalities_declared(PassthroughNormalizer()) == frozenset()
    assert ensure_structured_modalities_declared(
        VideoNormalizer(backend=lambda payload: ([], []))
    ) == frozenset({Modality.VIDEO})
