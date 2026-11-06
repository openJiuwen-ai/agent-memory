"""Fine-grained source governance: quarantine by range with an auditable reason."""

import pytest

from jiuwen_memory.common.errors import ValidationError

pytestmark = pytest.mark.unit


def test_quarantine_and_query_record(setup):
    service, scope, *_ = setup
    record = service.quarantine_source(scope, "workbuddy", "injected bad results", operator="gov")
    assert record["reason"] == "injected bad results"
    assert record["operator"] == "gov"
    assert record["quarantined_at"]
    assert service.source_quarantine(scope, "workbuddy") == record


def test_quarantine_is_scoped_and_does_not_affect_other_sources(setup):
    service, scope, *_ = setup
    service.quarantine_source(scope, "workbuddy", "reason")
    assert service.source_quarantine(scope, "hermes") is None


def test_quarantine_requires_a_non_empty_reason(setup):
    service, scope, *_ = setup
    with pytest.raises(ValidationError, match="reason"):
        service.quarantine_source(scope, "workbuddy", "   ")


def test_release_removes_quarantine_and_reports_status(setup):
    service, scope, *_ = setup
    service.quarantine_source(scope, "workbuddy", "reason")
    assert service.release_source(scope, "workbuddy") is True
    assert service.source_quarantine(scope, "workbuddy") is None
    assert service.release_source(scope, "workbuddy") is False
