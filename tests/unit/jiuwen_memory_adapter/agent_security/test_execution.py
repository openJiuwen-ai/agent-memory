"""首个 PR 只装配九问；其他框架不得回退到已有执行器。"""

import pytest

from jiuwen_memory_adapter.agent_security import ConfigurationError
from jiuwen_memory_adapter.agent_security.execution import create_execution_adapter

from .fixtures import guard, operation

pytestmark = pytest.mark.unit


def test_selection_does_not_import_framework():
    selected = create_execution_adapter(
        {"framework": "openjiuwen"}, guard(), operation_for=operation
    )
    assert selected.framework == "openjiuwen"
    selected.close()


@pytest.mark.parametrize("framework", ["unknown", "langchain", "openclaw"])
def test_unknown_or_deferred_framework_is_not_distributed(framework):
    with pytest.raises(ConfigurationError):
        create_execution_adapter({"framework": framework}, guard(), operation_for=operation)
