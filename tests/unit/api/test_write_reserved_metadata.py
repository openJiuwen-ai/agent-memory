"""写入/更新边界拒绝系统保留 metadata key。

索引投影用真源系统字段覆盖同名用户 metadata，而 UnitReader 复核 ``metadata.<key>``
读用户值——同名会让 Store 与真源判定相反、静默错筛。故在边界失败响亮。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import assemble
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import RESERVED_METADATA_KEYS, Modality, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import MemoryPatch

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")
_TEXT = "alice drinks iced americano every morning"


def _api():
    return assemble(config=Config.from_dict({}))


@pytest.mark.parametrize("key", ["lifecycle", "tier", "source", "tags", "unit_id"])
def test_add_rejects_reserved_metadata_key(key: str) -> None:
    api = _api()

    with pytest.raises(ValidationError):
        api.add(_TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR, metadata={key: "custom"})


def test_add_allows_normal_metadata() -> None:
    api = _api()

    units = api.add(
        _TEXT,
        _SCOPE,
        source=Modality.TEXT,
        identity=_ACTOR,
        metadata={"memory_type": "coding", "project": "alpha"},
    )

    assert units and units[0].metadata["project"] == "alpha"


def test_update_rejects_reserved_metadata_key() -> None:
    api = _api()
    unit = api.add(_TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR)[0]

    with pytest.raises(ValidationError):
        api.update(unit.id, _SCOPE, MemoryPatch(metadata={"lifecycle": "custom"}), identity=_ACTOR)


def test_add_preserves_scalar_types_end_to_end() -> None:
    """经完整写入路径（api.add → engine → RawPayload → Ingestor）后类型不被改写。

    回归护栏：engine.write 曾对整个 metadata 做 ``{k: str(v)}``，把数值/布尔拍成
    字符串——索引因此建成 keyword，range 退化为字典序（"10" < "9"），而这一层
    在直接构造 MemoryUnit 的测试里完全测不到。
    """
    api = _api()

    units = api.add(
        _TEXT,
        _SCOPE,
        source=Modality.TEXT,
        identity=_ACTOR,
        metadata={"priority": 8, "score": 9.5, "archived": False, "project": "alpha"},
    )

    meta = units[0].metadata
    assert meta["priority"] == 8 and not isinstance(meta["priority"], str)
    assert meta["score"] == 9.5
    assert meta["archived"] is False
    assert meta["project"] == "alpha"


def test_add_switch_accepts_native_bool() -> None:
    """调用级开关按字符串判定，传 Python True 与 "true" 等效。"""
    api = _api()

    units = api.add(
        _TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR, metadata={"procedural": True}
    )

    assert units  # 开关被识别、写入成功；未被识别时走的是另一条落库路径


@pytest.mark.parametrize("value", [{"nested": "dict"}, [1, 2, 3], ["mixed", 1]])
def test_add_rejects_non_scalar_metadata(value) -> None:
    """嵌套 dict / 非字符串数组在三个后端语义不一，入口挡住。"""
    api = _api()

    with pytest.raises(ValidationError):
        api.add(_TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR, metadata={"x": value})


def test_add_allows_string_array_metadata() -> None:
    """字符串数组是例外：有明确的成员包含语义（json_contains / term）。"""
    api = _api()

    units = api.add(
        _TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR, metadata={"langs": ["py", "go"]}
    )

    assert units[0].metadata["langs"] == ["py", "go"]


def test_reserved_keys_cover_index_projection_fields() -> None:
    # 保留集须覆盖索引投影会覆写的系统字段（与 construction 的 _index_metadata 对齐）
    assert {"unit_id", "tier", "lifecycle", "tags", "source", "content_layer"} <= (
        RESERVED_METADATA_KEYS
    )
    assert {"t_event", "t_valid", "t_invalid", "t_message", "seq"} <= RESERVED_METADATA_KEYS
