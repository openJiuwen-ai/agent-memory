# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实 HTTP Grant 流程；默认自建隔离服务，--server 可指定本机测试服务。"""

import argparse
from contextlib import nullcontext

from _support import logger, post, running

parser = argparse.ArgumentParser()
parser.add_argument("--server", help="已有 grant-dev.yml 测试服务；省略时自动启动隔离服务")
args = parser.parse_args()
context = nullcontext((None, args.server.rstrip("/"))) if args.server else running("grant-dev.yml")
with context as (_, base):
    alice = {"org": "demo", "user": "alice"}
    bob = {"org": "demo", "user": "bob"}
    units = post(base, "add", "demo-alice", {"content": "Grant HTTP test", "scope": alice})
    read = {"unit_id": units[0]["id"], "scope": alice}
    request = {"grant": {"grantor": alice, "grantee": bob, "actions": ["read"]}}
    post(base, "get", "demo-bob", read, 403)
    post(base, "grant", None, request, 401)
    post(base, "grant", "demo-bob", request, 403)
    logger.info("PASS before Grant: Bob -> 403; missing credential -> 401; forged grantor -> 403")
    created = post(base, "grant", "demo-alice", request)
    try:
        if not created["grant_id"]:
            raise RuntimeError("Grant creation must return a grant ID")
        unit = post(base, "get", "demo-bob", read)
        if unit["segments"][0]["content"] != "Grant HTTP test":
            raise RuntimeError("Bob must read the content after the Grant")
        post(
            base,
            "delete",
            "demo-bob",
            {
                "selector": {
                    "unit_ids": [units[0]["id"]],
                    "scope": alice,
                }
            },
            403,
        )
        post(base, "revoke", "demo-bob", {"grant": created}, 403)
        logger.info("PASS READ Grant: Bob reads -> 200; delete/revoke -> 403")
    finally:
        if post(base, "revoke", "demo-alice", {"grant": created}) is not None:
            raise RuntimeError("Grant revocation must return an empty result")
    post(base, "get", "demo-bob", read, 403)
    post(base, "get", "demo-alice", read)
    post(base, "add", "demo-bob", {"content": "Bob own data", "scope": bob})
    if post(base, "revoke", "demo-alice", {"grant": created}) is not None:
        raise RuntimeError("Repeated Grant revocation must return an empty result")
    post(base, "revoke", "demo-alice", request, 400)
    logger.info(
        "PASS revoke: Bob -> 403; owner/data/Key unaffected; repeat -> 200; missing ID -> 400"
    )
