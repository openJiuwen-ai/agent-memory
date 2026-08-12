"""search 的 Context 契约：max_tokens 经 Context.extensions 下达自适应披露。"""

from __future__ import annotations

import pytest

from api.memory_api_impl import assemble
from common.type_def import EXT_MAX_TOKENS, Context, Modality, Scope
from config import Config
from retrieval.types import DisclosureLevel

# discloser 用结构化披露（自适应分级）
_CONFIG = {"discloser": {"default": "structured"}}

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")
_TEXT = "alice loves iced americano coffee every single morning before work meetings and calls"


def _api():
    return assemble(config=Config.from_dict(_CONFIG))


def test_context_max_tokens_reaches_adaptive_disclosure() -> None:
    api = _api()
    api.add(_TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR)

    res = api.search(
        "coffee",
        Context(_SCOPE, extensions={EXT_MAX_TOKENS: "300"}),
        identity=_ACTOR,
        disclosure=DisclosureLevel.ADAPTIVE,
        with_trajectory=True,
    )

    disclose = next(s for s in res.trajectory if s.stage == "disclose")
    assert disclose.detail["max_tokens"] == "300"


def test_context_without_max_tokens_uses_default() -> None:
    api = _api()
    api.add(_TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR)

    res = api.search(
        "coffee",
        Context(_SCOPE),  # 不给预算 → max_tokens=None
        identity=_ACTOR,
        disclosure=DisclosureLevel.ADAPTIVE,
        with_trajectory=True,
    )

    disclose = next(s for s in res.trajectory if s.stage == "disclose")
    assert disclose.detail["max_tokens"] == ""  # 默认策略，无预算
