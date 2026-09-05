from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError
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
