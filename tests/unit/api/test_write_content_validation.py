"""写入边界拒绝非法 content（非 str / 空 / 纯空白）。

单条 add 曾漏校验：``None`` 冲到 engine ``encode`` 成 AttributeError，
``""`` 靠 Normalizer 对 ``b""`` 的假值副作用才成 ValidationError。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import assemble, build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Modality, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control import BatchWriteItem

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")


def _api():
    return assemble(config=Config.from_dict({}))


@pytest.mark.parametrize("content", [None, "", "   ", "\t\n"])
def test_add_rejects_invalid_content(content: object) -> None:
    api = _api()

    with pytest.raises(ValidationError, match="content must"):
        api.add(content, _SCOPE, source=Modality.TEXT, identity=_ACTOR)  # type: ignore[arg-type]


def test_add_accepts_non_empty_content() -> None:
    api = _api()

    units = api.add("remember this", _SCOPE, source=Modality.TEXT, identity=_ACTOR)

    assert units and units[0].content == "remember this"


@pytest.mark.parametrize("content", [None, "", "   "])
def test_batch_add_rejects_invalid_content(content: object) -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")

    result = api.batch_add(
        [BatchWriteItem(content=content), BatchWriteItem(content="valid")],  # type: ignore[arg-type]
        scope,
        identity=scope,
    )

    assert result.outcomes[0].error_type == "ValidationError"
    assert "content must" in result.outcomes[0].error
    assert result.outcomes[1].error == ""
    assert result.outcomes[1].units[0].content == "valid"
