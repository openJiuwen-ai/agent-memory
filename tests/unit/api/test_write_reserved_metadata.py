"""写入/更新边界拒绝系统保留 metadata key。

索引投影用真源系统字段覆盖同名用户 metadata，而 UnitReader 复核 ``metadata.<key>``
读用户值——同名会让 Store 与真源判定相反、静默错筛。故在边界失败响亮。
"""

from __future__ import annotations

import pytest

from api.memory_api_impl import assemble
from common.errors import ValidationError
from common.type_def import RESERVED_METADATA_KEYS, Modality, Scope
from config import Config
from control.types import MemoryPatch

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")
_TEXT = "alice drinks iced americano every morning"


def _api():
    return assemble(config=Config.from_dict({}))


@pytest.mark.parametrize("key", ["lifecycle", "tier", "source", "tags", "unit_id"])
def test_write_rejects_reserved_metadata_key(key: str) -> None:
    api = _api()

    with pytest.raises(ValidationError):
        api.write(_TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR, metadata={key: "custom"})


def test_write_allows_normal_metadata() -> None:
    api = _api()

    units = api.write(
        _TEXT,
        _SCOPE,
        source=Modality.TEXT,
        identity=_ACTOR,
        metadata={"memory_type": "coding", "project": "alpha"},
    )

    assert units and units[0].metadata["project"] == "alpha"


def test_update_rejects_reserved_metadata_key() -> None:
    api = _api()
    unit = api.write(_TEXT, _SCOPE, source=Modality.TEXT, identity=_ACTOR)[0]

    with pytest.raises(ValidationError):
        api.update(unit.id, _SCOPE, MemoryPatch(metadata={"lifecycle": "custom"}), identity=_ACTOR)


def test_reserved_keys_cover_index_projection_fields() -> None:
    # 保留集须覆盖索引投影会覆写的系统字段（与 construction 的 _index_metadata 对齐）
    assert {"unit_id", "tier", "lifecycle", "tags", "source", "content_layer"} <= (
        RESERVED_METADATA_KEYS
    )
    assert {"t_event", "t_valid", "t_invalid", "seq"} <= RESERVED_METADATA_KEYS
