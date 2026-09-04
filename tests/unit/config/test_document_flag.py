"""文档记忆开关归一（``config.document_flag``）——装配期 fail-closed 边界。

三个纯函数是「文档模式 / 向量开关 → 缺省实现」的判定中枢，被 engine / evolver /
job_factory / pipeline_retriever / composite_storage 多处装配期共用。失效方向分两型：

- ``should_write_document`` / ``resolve_watch_document`` 拼写错误（``"yes"`` 一类）
  若被静默当成 False/True，会整体吞掉或误开文档路径，且装配期无报错——必须抛
  :class:`ValidationError` fail-closed。
- ``resolve_index_builder_default`` 三处消费方若各自判定分叉，会让同一份装配拿到
  不一致的 IndexBuilder（文档模式错配 hybrid 即真源写错地方）。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config.document_flag import (
    WATCH_DOCUMENT_KEY,
    WRITE_DOCUMENT_KEY,
    resolve_index_builder_default,
    resolve_watch_document,
    should_write_document,
)

pytestmark = pytest.mark.unit


# -- should_write_document -------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        True,
        "true",
        "TRUE",
        "True",
        "1",
        "yes",
        "YES",
        "on",
        "ON",
        1,
        2,
        0.5,
    ],
)
def test_truthy_values_normalize_to_true(raw: object) -> None:
    assert should_write_document(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        False,
        "false",
        "FALSE",
        "False",
        "0",
        "no",
        "NO",
        "off",
        "OFF",
        0,
        0.0,
    ],
)
def test_falsy_values_normalize_to_false(raw: object) -> None:
    assert should_write_document(raw) is False


def test_none_means_unconfigured_defaults_to_false() -> None:
    """未配置（None）→ False，与 defaults 的 write_document=False 一致：仅写 KV。"""
    assert should_write_document(None) is False


def test_a_typoed_string_is_rejected_not_silently_closed() -> None:
    """拼写错误（如 ``"yes "`` 之外的 ``"yse"``）若吞成 False 会静默关闭文档路径。"""
    with pytest.raises(ValidationError, match="write_document"):
        should_write_document("yse")


def test_an_unrecognized_value_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        should_write_document(["true"])  # 列表不是可归一形态


def test_write_document_key_name_is_stable() -> None:
    """key 名是消费方各 _build 读取的契约，改 key 即装配分叉。"""
    assert WRITE_DOCUMENT_KEY == "write_document"
    assert WATCH_DOCUMENT_KEY == "watch_document"


# -- resolve_index_builder_default ------------------------------------------ #


def test_document_mode_wins_over_vector_flag() -> None:
    """文档模式（write_document=true）必须取 document，无视 vector_enabled。"""
    assert (
        resolve_index_builder_default({"write_document": True, "vector_enabled": True})
        == "document"
    )
    assert (
        resolve_index_builder_default({"write_document": "true", "vector_enabled": False})
        == "document"
    )


def test_non_document_mode_follows_vector_flag() -> None:
    assert resolve_index_builder_default({"vector_enabled": True}) == "hybrid"
    assert resolve_index_builder_default({"vector_enabled": False}) == "fulltext"


def test_non_document_mode_defaults_to_hybrid() -> None:
    """vector_enabled 未配置默认 True → hybrid（与 defaults 一致）。"""
    assert resolve_index_builder_default({}) == "hybrid"


# -- resolve_watch_document ------------------------------------------------- #


def test_unconfigured_watch_document_defaults_to_true() -> None:
    """未配置（None）→ True：开了文档就该监听 md 漂移（随文档开启）。"""
    assert resolve_watch_document(None) is True


def test_explicit_watch_document_is_respected() -> None:
    assert resolve_watch_document(True) is True
    assert resolve_watch_document(False) is False
    assert resolve_watch_document("false") is False
    assert resolve_watch_document("off") is False
    assert resolve_watch_document("true") is True


def test_a_typoed_watch_document_is_rejected() -> None:
    """watch_document 拼写错误同样 fail-closed，不静默落到默认 True。"""
    with pytest.raises(ValidationError, match="watch_document"):
        resolve_watch_document("tru")
