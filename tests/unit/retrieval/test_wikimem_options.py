"""wikimem retrieval option compatibility tests."""

from __future__ import annotations

import pytest

from common.errors import ValidationError
from retrieval.wikimem_options import (
    WikimemDirectory,
    WikimemRetrievalOptions,
    parse_wikimem_options,
)

pytestmark = pytest.mark.unit


def test_parse_wikimem_options_reads_json_lists_and_defaults() -> None:
    options = parse_wikimem_options(
        {
            "wikimem.recent_tools": '["Read", "Edit"]',
            "wikimem.already_surfaced_file_paths": '["docs/old.md"]',
            "wikimem.memory_dirs": '[{"scope": "auto", "path": "C:/repo/.memory"}]',
        }
    )

    assert options == WikimemRetrievalOptions(
        recent_tools=["Read", "Edit"],
        already_surfaced_file_paths=["docs/old.md"],
        include_entrypoints=False,
        memory_dirs=[WikimemDirectory(scope="auto", path="C:/repo/.memory")],
        memory_parallelism=None,
    )


def test_parse_wikimem_options_reads_bool_int_and_profile_fields() -> None:
    options = parse_wikimem_options(
        {
            "wikimem.include_entrypoints": "true",
            "wikimem.memory_parallelism": "0",
            "wikimem.profile": "memdir",
            "wikimem.selector_model": "primary",
            "wikimem.selector_fallback_model": "fallback",
            "max_tokens": "128",
        }
    )

    assert options.include_entrypoints is True
    assert options.memory_parallelism == 1
    assert options.profile == "memdir"
    assert options.selector_model == "primary"
    assert options.selector_fallback_model == "fallback"


@pytest.mark.parametrize(
    ("extensions", "message"),
    [
        ({"wikimem.recent_tools": "Read,Edit"}, "wikimem.recent_tools"),
        ({"wikimem.already_surfaced_file_paths": "[1]"}, "string array"),
        ({"wikimem.memory_dirs": '[{"scope": "global", "path": "x"}]'}, "scope"),
        ({"wikimem.memory_dirs": '[{"scope": "auto", "path": ""}]'}, "path"),
        ({"wikimem.include_entrypoints": "yes"}, "include_entrypoints"),
        ({"wikimem.memory_parallelism": "many"}, "memory_parallelism"),
    ],
)
def test_parse_wikimem_options_rejects_invalid_transport_values(
    extensions: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_wikimem_options(extensions)
