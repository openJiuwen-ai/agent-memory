"""Named encrypted private Store plugs into the existing human disclosure flow."""

import pytest

from jiuwen_memory.api import ValidationError, assemble_server_runtime
from tests.integration.security.test_memory_privacy import (
    config,
)
from tests.integration.security.test_memory_privacy import (
    test_human_view_restores_complete_sentence_and_never_changes_memory as exercise_human_view,
)

pytestmark = pytest.mark.integration


def settings(tmp_path, target="encrypted", *, backend="sqlite"):
    result = config(tmp_path, human=True)
    result["privacy"]["default"]["params"] = {"privacy_store": "private"}
    params = {"db_path": str(tmp_path / "private.sqlite")}
    if target == "encrypted":
        params = {
            "raw_privacy_store": {
                "target": "kv",
                "params": {"kv_store": {"target": backend, "params": params}},
            },
            "cryptography": "private",
        }
    result["privacy_store"] = {"private": {"target": target, "params": params}}
    return result


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_named_encrypted_store_supports_masked_reads_and_verified_human_view(tmp_path, backend):
    runtime = assemble_server_runtime(config=settings(tmp_path, backend=backend))
    try:
        exercise_human_view(runtime)
    finally:
        runtime.close()


def test_unencrypted_private_store_is_rejected(tmp_path):
    with pytest.raises(ValidationError, match="encrypted PrivacyStore"):
        assemble_server_runtime(config=settings(tmp_path, target="sqlite"))
