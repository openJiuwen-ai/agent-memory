"""展开参数在公开选项和内部请求边界保持严格语义。"""

import pytest

from jiuwen_memory.api import HierarchyKind, SearchOptions
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.retrieval.types import RetrievalQuery

pytestmark = pytest.mark.unit


def test_expand_defaults_to_zero() -> None:
    assert SearchOptions().expand_depth == 0
    assert RetrievalQuery().expand_depth == 0


@pytest.mark.parametrize("depth", [-1, True, False, 1.5, "1", None])
def test_internal_query_rejects_invalid_depth(depth) -> None:
    with pytest.raises(ValidationError, match="expand_depth"):
        RetrievalQuery(hierarchy_kind=HierarchyKind.TIME, expand_depth=depth)


def test_expansion_requires_explicit_kind() -> None:
    with pytest.raises(ValidationError, match="hierarchy_kind"):
        RetrievalQuery(expand_depth=1)


def test_positive_depth_does_not_require_a_parent_role_or_window() -> None:
    query = RetrievalQuery(hierarchy_kind=HierarchyKind.TIME, expand_depth=3)
    assert query.expand_depth == 3
    assert query.hierarchy_role is None
