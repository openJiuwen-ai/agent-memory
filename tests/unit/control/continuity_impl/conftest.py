"""S10 behavior: role paths, revocation, retries and restart using real local stores."""

import pytest

from jiuwen_memory.common.errors import ConflictError, PermissionDeniedError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import (
    MemoryRef,
)
from jiuwen_memory.construction.index_builder_impl.evidence_graph import EvidenceGraphBuilder
from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
from jiuwen_memory.control.continuity_impl.service import ContinuityService
from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from tests.support.connector_policies import AllowAllConnectorPolicy


@pytest.fixture
def setup(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    graph = InMemoryGraphStore()
    service = ContinuityService(
        ContinuityRepository(kv, single_writer=True),
        graph,
        RoleViewBuilder(),
        projector=EvidenceGraphBuilder(graph),
    )
    # 控制层测试不覆盖 connector 治理（由 API PEP 用例覆盖）；此处注入宽松策略替身。
    real_task = service.task

    def task(scope, request, owner, read, *, connector_policy=None):
        return real_task(
            scope,
            request,
            owner,
            read,
            connector_policy=connector_policy or AllowAllConnectorPolicy(),
        )

    service.task = task
    scope = Scope(org="acme", space="project")
    units = {}
    denied = set()

    def write(text, metadata=None):
        unit = MemoryUnit(
            id=str(len(units) + 1),
            scope=scope,
            segments=[Segment(content=text)],
            system_metadata=metadata or {},
        )
        units[unit.id] = unit
        return [unit]

    def read(ref):
        if ref.unit_id in denied:
            raise PermissionDeniedError("get")
        unit = units[ref.unit_id]
        if MemoryRef.of(unit) != ref:
            raise ConflictError("reference")
        return unit

    yield service, scope, write, read, denied, units
    kv.close()
