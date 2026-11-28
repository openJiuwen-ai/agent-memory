"""Policy switches in continuity config are strictly booleans (F17 D13)."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.control.continuity_impl.service import _policy_flag

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["true", 1, 0, None, [], {}])
def test_non_boolean_policy_values_are_rejected(value):
    with pytest.raises(ValidationError):
        _policy_flag({"self_bind": value}, "self_bind")


def test_missing_policy_flag_defaults_to_false():
    assert _policy_flag({}, "admin_manage_tasks") is False
    assert _policy_flag({"admin_manage_tasks": True}, "admin_manage_tasks") is True
