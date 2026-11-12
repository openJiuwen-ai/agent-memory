"""Snapshot retention, binding and one-use behavior at the privacy boundary."""

import copy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import RateLimitedError, ValidationError
from jiuwen_memory.common.security.privacy.snapshots import DisclosureSnapshots
from tests.unit.common.security.test_privacy import TEXT, protected_unit

pytestmark = pytest.mark.unit


def test_privacy_assembles_the_staged_snapshot(privacy, monkeypatch):
    unit = protected_unit(privacy)
    operation = privacy.stage_disclosure(unit)
    current = copy.deepcopy(unit)
    current.system_metadata["technical_counter"] = 2
    seen = []
    original = privacy.render

    def render(snapshot):
        seen.append(snapshot.system_metadata.copy())
        return original(snapshot)

    monkeypatch.setattr(privacy, "render", render)
    result = privacy.render_disclosure(operation, current)
    assert result.content == TEXT and "technical_counter" not in seen[0]
    assert "private-demo-value" not in repr(result)
    with pytest.raises(ValidationError, match="consumed"):
        privacy.render_disclosure(operation, current)


@pytest.mark.parametrize("change", ["scope", "id", "revision", "content"])
def test_changed_current_snapshot_is_consumed_without_decryption(privacy, change, monkeypatch):
    unit = protected_unit(privacy)
    operation = privacy.stage_disclosure(unit)
    current = copy.deepcopy(unit)
    if change == "scope":
        current.scope.user = "bob"
    elif change == "id":
        current.id = "other"
    elif change == "revision":
        current.system_metadata["privacy_revision"] = "a" * 32
    else:
        current.segments[0].content = "changed"

    def decrypt_forbidden(*args, **kwargs):
        pytest.fail("Mismatched snapshots must fail before rendering")

    monkeypatch.setattr(privacy, "render", decrypt_forbidden)
    with pytest.raises(ValidationError, match="changed"):
        privacy.render_disclosure(operation, current)
    with pytest.raises(ValidationError, match="consumed"):
        privacy.render_disclosure(operation, unit)


def test_pending_snapshot_expiry_capacity_and_cancellation(privacy, monkeypatch):
    unit = protected_unit(privacy)
    now = [10.0]
    monkeypatch.setattr(
        "jiuwen_memory.common.security.privacy.snapshots.time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    snapshots = DisclosureSnapshots(limit=1)
    operation = snapshots.stage(unit)
    with pytest.raises(RateLimitedError):
        snapshots.stage(unit)
    now[0] += 301
    with pytest.raises(ValidationError, match="expired"):
        snapshots.consume(operation)
    operation = snapshots.stage(unit)
    snapshots.discard(operation)
    with pytest.raises(ValidationError):
        snapshots.consume(operation)
    with pytest.raises(RateLimitedError):
        DisclosureSnapshots(max_bytes=1).stage(unit)


def test_competing_completions_release_only_one_result(privacy):
    unit = protected_unit(privacy)
    operation = privacy.stage_disclosure(unit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(privacy.render_disclosure, operation, unit) for _ in range(2)]
    assert sum(future.exception() is None for future in futures) == 1
    assert sum(isinstance(future.exception(), ValidationError) for future in futures) == 1
