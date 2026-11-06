"""`BatchWriteItem.source_ref` 必须在三个 Engine 实现里一致地传到 `write()`。

接续导入靠这条通道把 `source_locator` 映射到 `MemoryUnit.source_ref`。任一实现漏传，
用该引擎的部署就会丢掉原始来源定位——三种实现共用一个 Engine 契约，不能各写一份。
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import BatchWriteItem

pytestmark = pytest.mark.unit

_TARGETS = ("cloud", "agent", "in_memory")


def _engine(target: str):
    """完整装配后取引擎：缺省配置已把 ingestor/normalizer 接好，只需换 engine target。"""
    config = {
        "engine": {"default": {"target": target, "params": {}}},
        "kv_store": {"default": {"target": "memory", "params": {}}},
    }
    return _build_kernel(config=config).api._queries._engine


@pytest.mark.parametrize("target", _TARGETS)
def test_batch_write_forwards_source_ref_to_write(target):
    engine = _engine(target)
    # InMemoryEngine 只支持空 space 的 Scope；其余两种都可以。
    scope = Scope() if target == "in_memory" else Scope(org="acme", space="project")
    with patch.object(engine, "write", new=AsyncMock(return_value=[])) as spy:
        result = asyncio.run(
            engine.batch_write([BatchWriteItem("body", scope, source_ref="jira:REF-1")])
        )
    assert not result.outcomes[0].error, result.outcomes[0].error
    assert spy.await_args.kwargs["source_ref"] == "jira:REF-1", target
    assert spy.await_args.args[0] == "body"


@pytest.mark.parametrize("target", _TARGETS)
def test_batch_write_keeps_empty_source_ref(target):
    """没有 source_locator 时不得凭空造值：空串原样传下去，由 Ingestor 回落 payload id。"""
    engine = _engine(target)
    scope = Scope() if target == "in_memory" else Scope(org="acme", space="project")
    with patch.object(engine, "write", new=AsyncMock(return_value=[])) as spy:
        asyncio.run(engine.batch_write([BatchWriteItem("body", scope)]))
    assert spy.await_args.kwargs["source_ref"] == "", target
