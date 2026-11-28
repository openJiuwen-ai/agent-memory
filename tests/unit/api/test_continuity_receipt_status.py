"""The local-service receipt probe: in-process only, never routed by ``/v1``."""

import pytest

from jiuwen_memory.api import ContinuityWrite, ReceiptStatus, Scope, TaskRequest, assemble_runtime
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import SpaceSpec
from jiuwen_memory_entry.core.api_contract import api_method_names
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

PROJECT = Scope(org="acme", space="project")
OWNER = Scope(org="acme", user="owner")


def _config():
    return Config.from_dict(
        {
            "engine": {
                "default": {
                    "target": "cloud",
                    "params": dict.fromkeys(
                        (
                            "ingestor",
                            "retriever",
                            "evolver",
                            "index_builder",
                            "classifier",
                            "scheduler",
                            "policy",
                        ),
                        "default",
                    ),
                }
            },
            "authorizer": {
                "default": {"target": "space_aware", "params": {"delegate": "standard"}},
                "standard": {
                    "target": "standard",
                    "params": {"grant_store": "default", "delegation_store": "default"},
                },
            },
            "continuity": {
                "default": {
                    "target": "local",
                    "params": {
                        "single_writer": True,
                        "allow_ephemeral": True,
                        "mode": "continuity",
                    },
                }
            },
            "globals": {
                "continuity_connectors": {
                    "jiuwenswarm": {
                        "actor": {
                            "org": "acme",
                            "space": "",
                            "user": "owner",
                            "agent": "",
                            "session": "",
                        },
                        "projects": ["project"],
                        "enabled": True,
                    }
                }
            },
        }
    )


def test_receipt_status_is_trusted_in_process_only():
    runtime = assemble_runtime(config=_config())
    api = runtime.api
    security = internal_context(ScopedAuthenticator(OWNER, role=Role.USER))
    try:
        api.create_space(
            SpaceSpec(org="acme", space="project", owner=OWNER),
            security=internal_context(
                ScopedAuthenticator(Scope(org="acme", user="ops"), role=Role.ADMIN)
            ),
        )
        task = api.continuity_task(PROJECT, TaskRequest("create", "create"), security=security)
        bound = api.continuity_task(
            PROJECT,
            TaskRequest(
                "bind",
                "bind",
                task["task_id"],
                task["revision"],
                connector_id="jiuwenswarm",
                context_id="session",
            ),
            security=security,
        )
        binding_id = bound["task"]["bindings"][0]["binding_id"]
        api.add(
            "recorded event",
            PROJECT,
            security=security,
            continuity=ContinuityWrite(task["task_id"], binding_id, "event"),
        )
        receipt = api.continuity_receipt_status(
            PROJECT, task["task_id"], binding_id, "event"
        )
        assert isinstance(receipt, ReceiptStatus)
        assert receipt.status == "committed"
        assert receipt.operation_id and receipt.refs
        assert api.continuity_receipt_status(
            PROJECT, task["task_id"], binding_id, "never-happened"
        ) is None
        assert "continuity_receipt_status" not in api_method_names(), "receipt probe leaked to /v1"
    finally:
        runtime.close()
