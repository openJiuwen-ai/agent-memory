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


def _text_payload(text: str) -> RawPayload:
    return RawPayload(
        id="payload-split",
        scope=Scope(org="acme", user="alice"),
        modality=Modality.TEXT,
        data=text.encode("utf-8"),
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


# ---------------------------------------------------------------------------
# 超大文本预切分（BUG2026092802777）
# ---------------------------------------------------------------------------


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


def test_simple_ingestor_split_preserves_leading_separator() -> None:
    """首部空段落（前导分隔符）不因切分丢失：聚合路径拼接严格还原原文。"""
    text = "\n\n" + "\n\n".join(f"para{i} " + "x" * 40 for i in range(6))
    units = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=200).ingest(
        [_text_payload(text)]
    )

    assert len(units) > 1
    assert "\n\n".join(u.content for u in units) == text


def test_simple_ingestor_split_preserves_empty_paragraph_at_flush_boundary() -> None:
    """空段落恰在缓冲冲刷边界也不丢中间分隔符：拼接还原原文。"""
    # 段落序列 ["aaaa", 20×"b", "", "cc"]：第 2 段触发冲刷，随后空段落再触发一次。
    text = "aaaa\n\n" + "b" * 20 + "\n\n\n\ncc"
    units = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=20).ingest(
        [_text_payload(text)]
    )

    assert len(units) > 1
    assert "\n\n".join(u.content for u in units) == text


def test_simple_ingestor_sanitizes_injected_part_metadata() -> None:
    """用户注入的 _part/_parts 不透传：多片时强制覆盖为真实序号，单片时不残留。"""
    multi_payload = RawPayload(
        id="payload-inject",
        scope=Scope(org="acme", user="alice"),
        modality=Modality.TEXT,
        data=("\n\n".join(f"para{i} " + "x" * 40 for i in range(6))).encode("utf-8"),
        system_metadata={"_part": 999, "_parts": 42, "keep": "yes"},
    )
    units = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=200).ingest(
        [multi_payload]
    )

    assert len(units) > 1
    assert all(u.system_metadata["keep"] == "yes" for u in units)
    assert [u.system_metadata["_part"] for u in units] == list(
        range(1, len(units) + 1)
    )
    assert all(u.system_metadata["_parts"] == len(units) for u in units)

    single_payload = RawPayload(
        id="payload-inject-single",
        scope=Scope(org="acme", user="alice"),
        modality=Modality.TEXT,
        data=b"short",
        system_metadata={"_part": 7, "_parts": 9},
    )
    single = SimpleIngestor(PassthroughNormalizer(), max_unit_chars=0).ingest(
        [single_payload]
    )

    assert len(single) == 1
    assert "_part" not in single[0].system_metadata
    assert "_parts" not in single[0].system_metadata


def test_parse_max_unit_chars_rejects_invalid_values() -> None:
    """显式 null/空串/非整数非法值显式拒绝；缺省 8000；0/负数是文档化关闭开关。"""
    from jiuwen_memory.common.errors import ValidationError
    from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import _parse_max_unit_chars

    assert _parse_max_unit_chars(None) == 8000  # 键缺省 = 出厂默认
    assert _parse_max_unit_chars("8000") == 8000  # 配置变量展开后的整数字符串
    assert _parse_max_unit_chars(0) == 0  # 文档化关闭开关，不是非法值
    with pytest.raises(ValidationError):
        _parse_max_unit_chars("")  # 空串会静默关闭切分，必须显式拒绝
    with pytest.raises(ValidationError):
        _parse_max_unit_chars("abc")
    with pytest.raises(ValidationError):
        _parse_max_unit_chars(True)
