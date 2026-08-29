"""system_metadata / user_metadata 写入边界与隔离语义。"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import assemble
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    MemoryUnit,
    Modality,
    Scope,
    inherited_system_metadata,
    inherited_user_metadata,
)
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import MemoryPatch

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")
_TEXT = "alice drinks iced americano every morning"


def _api():
    return assemble(config=Config.from_dict({}))


def test_add_keeps_same_key_in_independent_namespaces() -> None:
    unit = _api().add(
        _TEXT,
        _SCOPE,
        source=Modality.TEXT,
        security=legacy_request_context(_ACTOR),
        system_metadata={"infer": False, "project": "system"},
        user_metadata={"infer": True, "project": "user"},
    )[0]

    assert unit.system_metadata["infer"] is False
    assert unit.user_metadata["infer"] is True
    assert unit.system_metadata["project"] == "system"
    assert unit.user_metadata["project"] == "user"


def test_user_metadata_never_controls_system_write_branch() -> None:
    unit = _api().add(
        _TEXT,
        _SCOPE,
        security=legacy_request_context(_ACTOR),
        user_metadata={"infer": True, "procedural": True, "middle": True},
    )[0]

    assert unit.content == _TEXT
    assert unit.tier.value == "episodic"


def test_system_infer_controls_write_while_same_user_key_does_not() -> None:
    units = _api().add(
        _TEXT,
        _SCOPE,
        security=legacy_request_context(_ACTOR),
        system_metadata={"infer": True},
        user_metadata={"infer": False},
    )

    assert units
    assert all(unit.provenance for unit in units)
    assert all(unit.user_metadata["infer"] is False for unit in units)
    assert all("infer" not in unit.system_metadata for unit in units)


def test_update_merges_namespaces_independently() -> None:
    api = _api()
    unit = api.add(
        _TEXT,
        _SCOPE,
        security=legacy_request_context(_ACTOR),
        system_metadata={"pipeline": "default"},
        user_metadata={"project": "alpha"},
    )[0]

    updated = api.update(
        unit.id,
        _SCOPE,
        MemoryPatch(
            system_metadata={"dreaming": True},
            user_metadata={"project": "beta", "owner": "alice"},
        ),
        security=legacy_request_context(_ACTOR),
    )

    assert updated.system_metadata == {"pipeline": "default", "dreaming": True}
    assert updated.user_metadata == {"project": "beta", "owner": "alice"}


@pytest.mark.parametrize("field_name", ["system_metadata", "user_metadata"])
@pytest.mark.parametrize("value", [{"nested": "dict"}, [1, 2, 3], ["mixed", 1]])
def test_add_rejects_unsupported_metadata_values(field_name: str, value) -> None:
    kwargs = {field_name: {"x": value}}
    with pytest.raises(ValidationError):
        _api().add(_TEXT, _SCOPE, security=legacy_request_context(_ACTOR), **kwargs)


def test_user_metadata_filter_uses_explicit_namespace() -> None:
    api = _api()
    api.add(
        _TEXT, _SCOPE, security=legacy_request_context(_ACTOR), user_metadata={"project": "alpha"}
    )

    result = api.list(
        _SCOPE,
        security=legacy_request_context(_ACTOR),
        filters=FilterClause("user_metadata.project", FilterOp.EQ, "alpha"),
    )

    assert result.count == 1
    assert result.items[0].user_metadata == {"project": "alpha"}


def test_legacy_metadata_filter_namespace_is_rejected() -> None:
    with pytest.raises(ValidationError, match="user_metadata"):
        _api().list(
            _SCOPE,
            security=legacy_request_context(_ACTOR),
            filters=FilterClause("metadata.project", FilterOp.EQ, "alpha"),
        )


@pytest.mark.parametrize("key", ["", "   ", 1])
def test_metadata_key_must_be_non_empty_string(key) -> None:
    with pytest.raises(ValidationError, match="key"):
        _api().add(_TEXT, _SCOPE, security=legacy_request_context(_ACTOR), user_metadata={key: "x"})


def test_metadata_rejects_non_finite_float() -> None:
    with pytest.raises(ValidationError, match="有限"):
        _api().add(
            _TEXT,
            _SCOPE,
            security=legacy_request_context(_ACTOR),
            user_metadata={"score": float("inf")},
        )


def test_derived_metadata_keeps_equal_user_values_and_drops_control_keys() -> None:
    first = MemoryUnit(
        system_metadata={"infer": True, "pipeline": "chat"},
        user_metadata={"project": "alpha", "priority": 1},
    )
    second = MemoryUnit(
        system_metadata={"infer": False, "pipeline": "chat"},
        user_metadata={"project": "alpha", "priority": 2},
    )

    assert inherited_user_metadata([first, second]) == {"project": "alpha"}
    assert inherited_system_metadata([first, second]) == {"pipeline": "chat"}
