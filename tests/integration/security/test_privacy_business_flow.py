"""Ported business classification uses the existing Store and human authorization flow."""

import json
from dataclasses import asdict

import pytest

from jiuwen_memory.api import ValidationError, assemble_server_runtime
from tests.integration.security.test_memory_privacy import INVITE, SCOPE, config, security
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(tmp_path):
    instance = assemble_server_runtime(config=config(tmp_path, human=True))
    yield instance
    instance.close()


def test_new_rule_evidence_and_human_disclosure_use_the_existing_authorization_flow(runtime):
    secret = "AKIA" + "A" * 16
    text = "新凭据 " + secret + "，联系 13800138000。"
    unit = runtime.api.add(text, SCOPE, security=security())[0]
    assert secret not in unit.content and unit.system_metadata["privacy_level"] == "S3"
    assert "credential.aws_key" in unit.system_metadata["privacy_categories"]
    events = [event for event in runtime.audit.query({}) if event.action == "privacy_protection"]
    assert any("aws_access_key" in event.detail["rule_ids"] for event in events)
    assert secret not in str(events)
    target = {
        "scope": asdict(SCOPE),
        "unit_id": unit.id,
        "revision": unit.system_metadata["privacy_revision"],
    }
    key, view = VirtualPasskey(), runtime.human_view
    enrollment = view.begin({**target, "invite": INVITE}, "browser")
    assert view.finish(
        {
            **target,
            "challenge_id": enrollment["challenge_id"],
            "credential": key.response(enrollment, enroll=True),
        },
        "browser",
    )["enrolled"]
    challenge = view.begin(target, "browser", security=security())
    response = view.finish(
        {
            **target,
            "challenge_id": challenge["challenge_id"],
            "credential": key.response(challenge, counter=2),
        },
        "browser",
    )
    assert response["content"] == text
    assert secret not in runtime.api.get(unit.id, SCOPE, security=security()).content
    assert secret not in json.dumps(unit.system_metadata)


@pytest.mark.parametrize("key", ["privacy_rule_ids", "privacy_categories"])
def test_clients_cannot_self_assert_classification_evidence(runtime, key):
    with pytest.raises(ValidationError):
        runtime.api.add(
            "ordinary", SCOPE, system_metadata={key: "client-value"}, security=security()
        )
