"""Per-child summary spans remain bound to the exact body and ordered links."""

import json
from copy import deepcopy

import pytest

from jiuwen_memory.common.graphmem_structure import (
    DETAIL_SPANS_KEY,
    RECURSIVE_VERSION,
    encode_child_details,
    read_child_details,
)
from jiuwen_memory.common.type_def import HierarchyRole, Segment
from tests.unit.construction.graphmem_fixtures import multi_source_tree

pytestmark = pytest.mark.unit


def detail_fixture():
    details = ["Alice 去了北京。", "Bob stayed home.\nHe read a book.", "Alice 去了北京。"]
    parent, children = multi_source_tree("details", details)
    parent.segments = [Segment(content="span"), Segment(content="\n".join(dict.fromkeys(details)))]
    parent.system_metadata.update(graphmem_recursive_version=RECURSIVE_VERSION,
        graphmem_height="1", graphmem_child_roles=[HierarchyRole.SNAPSHOT.value] * len(children))
    parent.system_metadata[DETAIL_SPANS_KEY] = encode_child_details(parent, details)
    return parent, children, details


def test_unicode_newlines_and_duplicate_details_round_trip_without_text_copy():
    parent, _, details = detail_fixture()
    assert list(read_child_details(parent).values()) == details
    data = json.loads(parent.system_metadata[DETAIL_SPANS_KEY])
    assert data["spans"][0] == data["spans"][2]
    assert set(data) == {"version", "binding", "spans"}


@pytest.mark.parametrize("change", ["text", "order", "scope", "role", "count"])
def test_changed_body_or_link_binding_invalidates_optional_map(change):
    parent, _, _ = detail_fixture()
    if change == "text":
        parent.segments[1].content += " altered"
    elif change == "order":
        parent.hierarchy.child_ids.reverse()
    elif change == "scope":
        parent.hierarchy.child_scopes[0] = deepcopy(parent.scope)
    elif change == "role":
        parent.system_metadata["graphmem_child_roles"][0] = HierarchyRole.TIME_SPAN.value
    else:
        parent.hierarchy.child_ids.pop()
    assert read_child_details(parent) == {}


@pytest.mark.parametrize("span", [[True, 2], [-1, 2], [2, 1], [0, 100000], [1], "0:1"])
def test_invalid_span_never_selects_text(span):
    parent, _, _ = detail_fixture()
    data = json.loads(parent.system_metadata[DETAIL_SPANS_KEY])
    data["spans"][0] = span
    parent.system_metadata[DETAIL_SPANS_KEY] = json.dumps(data)
    assert not read_child_details(parent)


def test_old_nodes_and_unrelated_details_fail_closed():
    parent, _, details = detail_fixture()
    del parent.system_metadata[DETAIL_SPANS_KEY]
    assert not read_child_details(parent)
    with pytest.raises(ValueError, match="complete summary"):
        encode_child_details(parent, [*details[:-1], "Invented detail"])
