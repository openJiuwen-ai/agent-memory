"""写入边界拒绝非法 content（非 str / 空 / 纯空白）。

单条 write 曾漏校验：``None`` 冲到 engine ``encode`` 成 AttributeError，
``""`` 靠 Normalizer 对 ``b""`` 的假值副作用才成 ValidationError。
"""

from __future__ import annotations

import pytest

from api.memory_api_impl import assemble, build_kernel
from common.errors import ValidationError
from common.type_def import Modality, Scope
from config import Config
from control import BatchWriteItem

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")


def _api():
    return assemble(config=Config.from_dict({}))


@pytest.mark.parametrize("content", [None, "", "   ", "\t\n"])
def test_write_rejects_invalid_content(content: object) -> None:
    api = _api()

    with pytest.raises(ValidationError, match="content must"):
        api.write(content, _SCOPE, source=Modality.TEXT, identity=_ACTOR)  # type: ignore[arg-type]


def test_write_accepts_non_empty_content() -> None:
    api = _api()

    units = api.write("remember this", _SCOPE, source=Modality.TEXT, identity=_ACTOR)

    assert units and units[0].content == "remember this"


@pytest.mark.parametrize("content", [None, "", "   "])
def test_batch_write_rejects_invalid_content(content: object) -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")

    result = api.batch_write(
        [BatchWriteItem(content=content), BatchWriteItem(content="valid")],  # type: ignore[arg-type]
        scope,
        identity=scope,
    )

    assert result.outcomes[0].error_type == "ValidationError"
    assert "content must" in result.outcomes[0].error
    assert result.outcomes[1].error == ""
    assert result.outcomes[1].units[0].content == "valid"
