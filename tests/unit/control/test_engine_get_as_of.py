from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api import DeleteMode, DeleteSelector, MemoryPatch, Scope
from api.memory_api_impl import build_kernel
from common.errors import NotFoundError
from common.type_def import MemoryTier, MemoryUnit, Modality, Segment, Temporal, memory_key
from common.type_def.memory_codec import dumps


def test_get_as_of_returns_version_valid_at_that_time() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    first_valid = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    second_valid = datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc)
    old = MemoryUnit(
        id="home-v1",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Shanghai", source=Modality.TEXT)],
        temporal=Temporal(t_valid=first_valid, t_invalid=second_valid),
    )
    new = MemoryUnit(
        id="home-v2",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Beijing", source=Modality.TEXT)],
        temporal=Temporal(t_valid=second_valid),
        supersedes=old.id,
    )
    kernel.kv.insert(scope, memory_key(old.id), dumps(old))
    kernel.kv.insert(scope, memory_key(new.id), dumps(new))

    before_update = kernel.api.get(
        new.id,
        scope,
        identity=actor,
        as_of=datetime(2026, 6, 17, 10, 30, tzinfo=timezone.utc),
    )
    after_update = kernel.api.get(
        old.id,
        scope,
        identity=actor,
        as_of=datetime(2026, 6, 17, 11, 30, tzinfo=timezone.utc),
    )

    assert before_update.id == old.id
    assert before_update.content == "home is Shanghai"
    assert after_update.id == new.id
    assert after_update.content == "home is Beijing"


def test_get_as_of_handles_historical_update_before_original_write_time() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()

    old = kernel.api.write("home is Shanghai", scope, identity=actor)[0]
    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(
            content="home is Beijing",
            t_valid=datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc),
        ),
        identity=actor,
    )

    before_update = kernel.api.get(
        new.id,
        scope,
        identity=actor,
        as_of=datetime(2026, 6, 17, 10, 30, tzinfo=timezone.utc),
    )
    after_update = kernel.api.get(
        old.id,
        scope,
        identity=actor,
        as_of=datetime(2026, 6, 17, 11, 30, tzinfo=timezone.utc),
    )

    assert before_update.id == old.id
    assert before_update.content == "home is Shanghai"
    assert after_update.id == new.id
    assert after_update.content == "home is Beijing"


def test_get_as_of_does_not_return_forgotten_version() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    old_valid = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    new_valid = datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc)

    old = MemoryUnit(
        id="home-v1",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Shanghai", source=Modality.TEXT)],
        temporal=Temporal(t_valid=old_valid, t_invalid=new_valid),
    )
    new = MemoryUnit(
        id="home-v2",
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="home is Beijing", source=Modality.TEXT)],
        temporal=Temporal(t_valid=new_valid),
        supersedes=old.id,
    )
    kernel.kv.insert(scope, memory_key(old.id), dumps(old))
    kernel.kv.insert(scope, memory_key(new.id), dumps(new))
    kernel.api.delete(
        DeleteSelector(unit_ids=[old.id], scope=scope, mode=DeleteMode.FORGET),
        identity=actor,
    )

    with pytest.raises(NotFoundError):
        kernel.api.get(
            new.id,
            scope,
            identity=actor,
            as_of=datetime(2026, 6, 17, 10, 30, tzinfo=timezone.utc),
        )
