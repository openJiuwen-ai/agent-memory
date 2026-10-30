# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""独立配置有限委托，验证真实 agent、代写作者和旧上下文在线撤销。"""

import hashlib
from datetime import UTC, datetime, timedelta

from jiuwen_memory.api import (
    Credentials,
    PermissionDeniedError,
    Scope,
    assemble_server_runtime,
)
from jiuwen_memory.common.security.capabilities import delegation_sources

selector = "demo-agent"
credential_id = hashlib.sha256(selector.encode()).hexdigest()
agent_scope = {"org": "demo", "agent": "agent1", "session": "session1"}
alice_scope = {"org": "demo", "user": "alice"}
config = {
    "security": {
        "default": {
            "target": "standard",
            "params": {
                "authenticator": "demo",
                "authorizer": "default",
                "delegation_store": "default",
                "delegation_bindings": {credential_id: "alice-agent1"},
                "delegations": [
                    {
                        "delegation_id": "alice-agent1",
                        "delegator": alice_scope,
                        "delegate": agent_scope,
                        "actions": ["read", "write"],
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                        "bound_credential_id": credential_id,
                        "bound_session": "session1",
                    }
                ],
            },
        }
    },
    "authenticator": {
        "demo": {
            "target": "dev",
            "params": {
                "identities": {
                    "demo-alice": {"actor": alice_scope, "role": "user"},
                    selector: {"actor": agent_scope, "role": "user"},
                }
            },
        }
    },
    "authorizer": {
        "default": {
            "target": "standard",
            "params": {
                "grant_store": "default",
                "delegation_store": "default",
            },
        }
    },
    "grant_store": {"default": {"target": "memory"}},
    "delegation_store": {"default": {"target": "memory"}},
}
runtime = assemble_server_runtime(config=config)
try:
    agent = runtime.authenticate(Credentials(api_key=selector))
    assert agent.auth.actor == Scope(**agent_scope)
    assert not agent.auth.actor.user
    print("PASS identity: caller remains agent1/session1")
    target = Scope(**alice_scope)
    unit = runtime.api.add("Delegated meeting notes", target, security=agent)[0]
    assert unit.system_metadata["author_principal"] == "user:alice"
    assert unit.system_metadata["author_agent"] == "agent1"
    assert runtime.api.get(unit.id, target, security=agent).content == "Delegated meeting notes"
    print("PASS delegation: agent writes/reads; author is Alice with agent1 recorded")

    # 服务端管理流程撤销当前 Authorizer 实际使用的同一委托 Store。
    stores = delegation_sources(runtime.security_runtime.authorizer)
    assert len(stores) == 1
    stores[0].revoke("alice-agent1")
    try:
        runtime.api.get(unit.id, target, security=agent)
    except PermissionDeniedError:
        print("PASS revoke: previously authenticated agent context is denied")
    else:
        raise AssertionError("Revoked delegation must deny the agent")
    alice = runtime.authenticate(Credentials(api_key="demo-alice"))
    assert runtime.api.get(unit.id, target, security=alice).content == "Delegated meeting notes"
    print("PASS owner: Alice can still read; data was not deleted")
finally:
    runtime.close(wait=True)
