# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RawPayload must not silently prefer data over uri when both are set."""

import pytest

from jiuwen_memory.common.type_def import RawPayload


def test_data_and_uri_together_are_rejected() -> None:
    with pytest.raises(ValueError, match="both data and uri"):
        RawPayload(data=b"body", uri="file:///source.txt")


def test_single_source_still_works() -> None:
    assert RawPayload(data=b"body").data == b"body"
    assert RawPayload(uri="file:///source.txt").uri == "file:///source.txt"
