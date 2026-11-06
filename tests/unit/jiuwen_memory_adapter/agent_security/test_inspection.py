"""公开无解密检测端口与既有隐私规则同源，写入豁免不影响外发掩码。"""

import pytest

from jiuwen_memory_adapter.agent_security.inspection import InspectionError, create_text_inspector

pytestmark = pytest.mark.unit


def test_plugin_detector_has_no_reveal_or_staging():
    inspector = create_text_inspector()
    result = inspector.inspect("phone=13800138000; api_key=secret-value")
    assert result.level_id == "S3"
    assert "13800138000" not in result.redacted and "secret-value" not in result.redacted
    assert not hasattr(inspector, "render") and not hasattr(inspector, "store")


def test_identity_default_shape_and_optional_checksum_follow_pr406():
    text = "110105194912310021"  # 最后一位错误，但形态仍需要保护。
    default = create_text_inspector()
    strict = create_text_inspector(rule_overrides={"id_card_cn": {"validator": "id_card_cn"}})
    assert "id_card_cn" in default.inspect(text).matched_rules
    assert "id_card_cn" not in strict.inspect(text).matched_rules
    assert default.version != strict.version


def test_bank_card_luhn_and_field_context():
    inspector = create_text_inspector()
    assert "bank_card" in inspector.inspect("4111111111111111").matched_rules
    assert "bank_card" not in inspector.inspect("4111111111111112").matched_rules
    assert inspector.inspect("a-short-value", label="api_key").level_id == "S3"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": True},
        {"timeout": float("nan")},
        {"capacity": 0},
        {"rules": [{"pattern": "[", "category": "pii.test", "level": "S2"}]},
    ],
)
def test_invalid_detection_configuration_fails_closed(kwargs):
    with pytest.raises(InspectionError):
        create_text_inspector(**kwargs)


def test_existing_mask_does_not_exempt_adjacent_or_quoted_secret():
    inspector = create_text_inspector()
    for text in ["api_key=[PRIVATE]new-secret", 'api_key="[PRIVATE] new-secret"']:
        result = inspector.inspect(text)
        assert result.level_id == "S3" and "new-secret" not in result.redacted
        assert inspector.inspect(result.redacted).level_id == "S1"
