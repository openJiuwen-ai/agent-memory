"""postgres 部署配置以 PostgreSQL/pgvector 替换 Redis/Milvus。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_context

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_PROFILE = _ROOT / "deploy/docker/postgres"


def _context() -> AssemblyContext:
    payload = yaml.safe_load((_PROFILE / "config.yml").read_text(encoding="utf-8"))[
        "memory_api"
    ]
    return default_context().merged(AssemblyContext.from_dict(payload))


def test_postgres_profile_replaces_only_kv_and_vectors() -> None:
    ctx = _context()

    assert ctx.lookup("kv_store", "default").target == "postgres"
    vector_specs = [
        ctx.lookup("vector_store", name) for name in ("default", "layers_l0", "layers_l1")
    ]
    assert {spec.target for spec in vector_specs} == {"pgvector"}
    assert len({spec.params["table"] for spec in vector_specs}) == 3
    assert all(spec.params["auto_create_schema"] is False for spec in vector_specs)
    assert all(spec.params["create_extension"] is False for spec in vector_specs)
    kv_spec = ctx.lookup("kv_store", "default")
    assert kv_spec.params["auto_create_schema"] is False
    assert {
        spec.params["dsn"] for spec in [kv_spec, *vector_specs]
    } == {
        "postgresql://${POSTGRES_USER:-agent_memory}:"
        "${POSTGRES_PASSWORD:-agent_memory}@postgres:5432/"
        "${POSTGRES_DB:-agent_memory}"
    }
    assert {
        ctx.lookup("fulltext_store", name).target
        for name in ("default", "layers_l0", "layers_l1")
    } == {"elasticsearch"}


def test_postgres_profile_compose_starts_required_backends() -> None:
    compose = yaml.safe_load((_PROFILE / "docker-compose.yml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == {"agent-memory", "elasticsearch", "postgres"}
    assert set(compose["volumes"]) == {"es-data", "postgres-data"}

    postgres = compose["services"]["postgres"]
    assert postgres["image"] == "pgvector/pgvector:0.8.3-pg16"
    assert any(
        str(mount).endswith(
            "scripts/pg_schema.sql:/docker-entrypoint-initdb.d/10-agent-memory.sql:ro"
        )
        for mount in postgres["volumes"]
    )

    dependencies = compose["services"]["agent-memory"]["depends_on"]
    assert dependencies["postgres"]["condition"] == "service_healthy"
    assert dependencies["elasticsearch"]["condition"] == "service_healthy"
