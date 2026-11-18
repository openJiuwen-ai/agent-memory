from __future__ import annotations

from threading import Lock
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import ValidationError
from jiuwen_memory_entry.portal.multi_agent import VIEW_VALUE
from jiuwen_memory_entry.portal.shared import _BrowserAccess, _Registration, _Registry


def _access() -> _BrowserAccess:
    return _BrowserAccess(
        identity="identity",
        preferred_instance="default",
        registration="registration",
        write_scope=None,
        provider_bound=False,
    )


def test_multi_agent_view_forwards_portal_context() -> None:
    registry = object.__new__(_Registry)
    registry.lock = Lock()
    source = _Registration("registration", "default", "identity", None)
    registry.registrations = {source.endpoint: source}
    registry.forward = Mock(return_value=(200, {"available": True}))
    access = _access()

    status, result = registry.dispatch("portal_context", {}, access, None, view=VIEW_VALUE)

    assert (status, result) == (200, {"available": True})
    registry.forward.assert_called_once_with(source, "portal_context", {}, access, VIEW_VALUE)


def test_multi_agent_view_rejects_config_verbs_before_instance_selection() -> None:
    registry = object.__new__(_Registry)
    registry.configuration = Mock(return_value=(200, {}))
    access = _access()

    with pytest.raises(ValidationError, match="read-only"):
        registry.dispatch(
            "save_config",
            {"instance_id": "default", "retry": True},
            access,
            None,
            view=VIEW_VALUE,
        )

    registry.configuration.assert_not_called()
