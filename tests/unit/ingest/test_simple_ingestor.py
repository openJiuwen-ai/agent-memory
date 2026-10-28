from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import UnsupportedCapabilityError
from jiuwen_memory.common.normalizer import Normalizer
from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
    PassthroughNormalizer,
)
from jiuwen_memory.common.type_def import Modality, RawPayload, Scope
from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor
from jiuwen_memory.ingest.source_impl.text_source import TextSource

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


def test_text_source_compares_naive_and_aware_incremental_boundaries_in_utc() -> None:
    earlier = datetime(2026, 1, 1, 8)
    later = datetime(2026, 1, 1, 10)
    source = TextSource(Scope(org="acme", user="alice"), [("old", earlier), ("new", later)])

    payloads = source.fetch(datetime(2026, 1, 1, 9, tzinfo=UTC))

    assert [payload.data for payload in payloads] == [b"new"]
    assert payloads[0].occurred_at is later


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


# -- 缺口B（issue #252）：默认直存链路应从 content 抽取事件时间写入 t_event ----- #
# 此前 t_event 硬编码 None，时间窗谓词的哨兵分支使日期过滤永远空转。


def _text_payload(content: str) -> RawPayload:
    return RawPayload(
        id="payload-date",
        scope=Scope(org="acme", user="alice"),
        modality=Modality.TEXT,
        data=content.encode("utf-8"),
    )


def test_simple_ingestor_extracts_event_time_from_content() -> None:
    units = SimpleIngestor(PassthroughNormalizer()).ingest(
        [_text_payload("2026年6月8日我和小王去了湘菜馆吃饭")]
    )

    assert units[0].temporal.t_event == datetime(2026, 6, 8, tzinfo=UTC)


def test_simple_ingestor_event_time_infers_year_from_ingest_time() -> None:
    units = SimpleIngestor(PassthroughNormalizer()).ingest(
        [_text_payload("6月8日我和小王去了湘菜馆吃饭")]
    )

    t_event = units[0].temporal.t_event
    assert t_event is not None
    assert (t_event.year, t_event.month, t_event.day) == (
        datetime.now(UTC).year,
        6,
        8,
    )


def test_simple_ingestor_without_date_keeps_t_event_none() -> None:
    units = SimpleIngestor(PassthroughNormalizer()).ingest(
        [_text_payload("我今天中午吃了麦当劳")]
    )

    assert units[0].temporal.t_event is None
