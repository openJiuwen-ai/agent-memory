# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""群体记忆 HTTP 测试：仅固定 LLM 输出，其他业务组件使用真实实现。"""

import json
import re
import urllib.error
import urllib.request

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM, LlmProducer

_SOURCE_PATTERN = re.compile(r"\[ID: ([^\]]+)\]\n(.*?)(?=\n\[ID: |\Z)", re.DOTALL)


class _CollectiveLLM(LLM):
    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.LLM

    @staticmethod
    def health() -> None:
        return None

    @staticmethod
    def chat(messages, **options) -> str:
        del options
        sources = _SOURCE_PATTERN.findall(messages[-1].content)
        if not sources:
            pytest.fail("real extractor/router must supply source IDs")
        items = []
        routing = "CLASSES:" in messages[0].content
        for source_id, content in sources:
            if routing:
                memory_class = "team_convention" if "团队" in content else "user_pref"
                items.append({
                    "source_id": source_id, "memory_class": memory_class,
                    "narrow": {
                        "agent_id": False, "session_id": False,
                        "team_id": memory_class == "team_convention",
                    },
                })
            else:
                for extracted in ("用户习惯用 Python 写代码", "团队规定代码评审必须两人"):
                    items.append({
                        "source_id": source_id, "content": extracted,
                        "target": "fact", "tier": "semantic", "confidence": 1.0,
                    })
        return json.dumps(items, ensure_ascii=False)


@LlmProducer.register("http_collective_fixture")
def _build_collective_llm(_config):
    return _CollectiveLLM()


def collective_settings():
    components = (
        "ingestor", "index_builder", "retriever", "kv_store", "scheduler", "evolver", "lifecycle",
    )
    identities = {"test-ops": {"actor": {"org": "local"}, "role": "admin"}}
    for user in ("u1", "u2", "u3"):
        identities[f"test-{user}"] = {"actor": {"org": "local", "user": user}}
    identities["test-u1-agent"] = {
        "actor": {"org": "local", "user": "u1", "agent": "a1", "session": "s1"},
    }
    return {
        "http": {"dev_identities": identities},
        "memory_api": {
            "engine": {"default": {
                "target": "cloud", "params": dict.fromkeys(components, "default"),
            }},
            "permission": {"default": {"target": "space_aware", "params": {"db_path": ":memory:"}}},
            "llm": {"http_fixture": "http_collective_fixture"},
            "extractor": {"default": {"target": "llm", "params": {"llm": "http_fixture"}}},
            "router": {"default": {"target": "llm", "params": {
                "llm": "http_fixture", "coord_entities": ["team"],
                "memory_classes": [
                    {"name": "user_pref", "owner": "user", "space_template": "u-{user}",
                     "fallback": True},
                    {"name": "team_convention", "owner": "team", "space_template": "team-{team}",
                     "cross_user": True, "members": "team participants"},
                ],
                "narrow_dims": [
                    {"entity": "agent", "tag_key": "agent_id"},
                    {"entity": "session", "tag_key": "session_id"},
                    {"entity": "team", "tag_key": "team_id"},
                ],
                "retry_max_retries": 1,
            }}},
        },
    }


def post_as(url, token, method, payload):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{url}/v1/{method}", data=json.dumps(payload).encode(), headers=headers, method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def provision_spaces(url):
    for space, owner in (("u-u1", "u1"), ("u-u2", "u2"), ("u-u3", "u3"), ("team-t", "u3")):
        status, body = post_as(url, "test-ops", "create_space", {
            "spec": {"org": "local", "space": space, "owner": {"org": "local", "user": owner}},
        })
        if status != 200:
            pytest.fail(f"create_space({space}) failed: HTTP {status}; response={body}")
    for user in ("u1", "u2"):
        status, body = post_as(url, "test-u3", "add_space_member", {
            "org": "local", "space": "team-t", "member": {
                "scope": {"user": user}, "content_role": "contributor", "governance_role": "none",
            },
        })
        if status != 200:
            pytest.fail(f"add_space_member({user}) failed: HTTP {status}; response={body}")
