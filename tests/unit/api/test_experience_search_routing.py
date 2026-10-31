"""经验检索的路由与类型隔离（mem2.0 §2.3 检索/列举隔离）。

接口层在选定 profile 后追加类型谓词：无路由值（默认 profile）追加
``memory_type NOT_IN ["experience","skill"]``——对话检索不混入经验与技能条目；
路由值为隔离类型时追加等值谓词，只取该类。路由值解析与 profile 选择同一口径
（``extensions[route_key]`` 优先、其次 filters 的强制等值）。经验检索一次只查
一个空间：跨空间检索的路由值为 experience 时返回参数错误（技能检索侧放行，
按等值谓词读、权限沿用空间规则）。

真实装配下端到端验证（cloud 引擎 + 空间感知判定 + MetadataPipeline），
experience profile 的 retriever 用打标记的探针实现，断言路由落点与谓词效果。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import (
    Context,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config import Config
from jiuwen_memory.control import SpaceSpec
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

ORG = "acme"
EXP_SPACE = "shared-experience"
PLAIN_SPACE = "plain"
OPS = Scope(org=ORG, user="ops")
ALICE = Scope(org=ORG, user="alice")
EXP_SCOPE = Scope(org=ORG, space=EXP_SPACE)
PLAIN_SCOPE = Scope(org=ORG, space=PLAIN_SPACE)

_ENGINE_COMPONENT_NAMES = (
    "ingestor",
    "index_builder",
    "retriever",
    "kv_store",
    "scheduler",
    "evolver",
    "lifecycle",
)

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为


class _ProbeRetriever(Retriever):
    """经验 profile 的探针 retriever：记录每次检索的扩展参数，返回空结果。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        self.calls.append(
            {
                "space": scope.space,
                "memory_type": str(query.extensions.get("memory_type", "")),
            }
        )
        return RetrievalResult(items=[])


_PROBE = _ProbeRetriever()


@RetrieverProducer.register("experience_probe")
def _build_probe(config):
    return _PROBE


def _kernel():
    """cloud 引擎 + 空间感知判定 + 带 experience profile 的 MetadataPipeline。"""
    engine_params = {name: "default" for name in _ENGINE_COMPONENT_NAMES}
    return build_kernel(
        config=Config.from_dict(
            {
                "engine": {"default": {"target": "cloud", "params": engine_params}},
                "permission": {
                    "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
                },
                "authorizer": {
                    "default": {
                        "target": "space_aware",
                        "params": {"delegate": "standard"},
                    },
                    "standard": {
                        "target": "standard",
                        "params": {"grant_store": "default", "delegation_store": "default"},
                    },
                },
                "grant_store": {"default": "memory"},
                "delegation_store": {"default": "memory"},
                "retriever": {"experience_probe": {"target": "experience_probe", "params": {}}},
                "pipeline": {
                    "default": {
                        "target": "metadata",
                        "params": {
                            "profiles": {
                                "default": {
                                    "index_builder": "default",
                                    "retriever": "default",
                                    "evolver": "default",
                                },
                                "experience": {
                                    "index_builder": "default",
                                    "retriever": "experience_probe",
                                    "evolver": "default",
                                },
                            },
                            "fallback": "default",
                        },
                    }
                },
            }
        )
    )


@pytest.fixture
def api():
    kernel = _kernel()
    api = kernel.api
    api.create_space(
        SpaceSpec(org=ORG, space=EXP_SPACE, owner=ALICE),
        security=internal_context(ScopedAuthenticator(OPS, role=Role.ADMIN)),
    )
    api.create_space(
        SpaceSpec(org=ORG, space=PLAIN_SPACE, owner=ALICE),
        security=internal_context(ScopedAuthenticator(OPS, role=Role.ADMIN)),
    )
    _PROBE.calls.clear()
    return api


def _write_experience_unit(api, uid: str, content: str) -> None:
    """经验条目直落 KV 真源：experience 是不回落取值，写入入口拒绝直写
    （§2.3 类型不可伪造；经验由 extractor 产出），检索隔离要拿真条目验证。"""
    unit = MemoryUnit(
        id=uid,
        scope=EXP_SCOPE,
        segments=[Segment(content=content)],
        temporal=Temporal(),
        system_metadata={"memory_type": "experience"},
    )
    api._engine.kv.insert(EXP_SCOPE, memory_key(uid), dumps(unit))


# ====================================================================== #
# 单空间：路由值决定 profile 与类型谓词
# ====================================================================== #


def test_search_without_route_value_stays_on_default_profile(api) -> None:
    """无路由值：检索走默认 profile（经验 profile 的探针不被调用）。"""
    api.search(
        "启动服务",
        Context(scope=EXP_SCOPE),
        security=internal_context(ScopedAuthenticator(ALICE)),
    )
    assert _PROBE.calls == [], "无路由值不走经验 profile"


def test_search_with_experience_route_uses_experience_profile(api) -> None:
    """extensions 带 memory_type=experience：路由到经验 profile，扩展参数带路由值。"""
    result = api.search(
        "启动服务",
        Context(scope=EXP_SCOPE, extensions={"memory_type": "experience"}),
        security=internal_context(ScopedAuthenticator(ALICE)),
    )
    assert result.items == [], "探针 retriever 返回空结果"
    assert _PROBE.calls == [{"space": EXP_SPACE, "memory_type": "experience"}], (
        "检索落在经验 profile 的 retriever"
    )


def test_search_with_route_value_from_filters(api) -> None:
    """filters 点名 memory_type 等值：与 extensions 同一解析口径，同样路由到经验
    profile——谓词与 profile 选择各解析一套即「选了经验 profile 却被自己的谓词
    排除经验」。
    """
    api.search(
        "启动服务",
        Context(scope=EXP_SCOPE),
        security=internal_context(ScopedAuthenticator(ALICE)),
        filters={"system_metadata.memory_type": "experience"},
    )
    assert _PROBE.calls and _PROBE.calls[0]["space"] == EXP_SPACE, (
        "filters 的强制等值同样构成路由值：检索落在经验 profile"
    )


# ====================================================================== #
# 单空间：默认检索不混入经验条目（NOT_IN 谓词）
# ====================================================================== #


def test_default_search_excludes_experience_entries(api) -> None:
    """无路由值的默认检索不返回经验条目——接口层按 NOT_IN 排除两类隔离条目。"""
    _write_experience_unit(api, "exp-probe-unit", "部署在共享空间的环境事实")
    plain = api.add(
        "一条普通的对话记忆",
        EXP_SCOPE,
        security=internal_context(ScopedAuthenticator(ALICE)),
    )[0]

    result = api.search(
        "记忆",
        Context(scope=EXP_SCOPE),
        security=internal_context(ScopedAuthenticator(ALICE)),
        top_k=10,
    )

    returned = {item.unit_id for item in result.items}
    assert plain.id in returned, "对话条目照常返回"
    assert "exp-probe-unit" not in returned, "经验条目被 NOT_IN 谓词排除"


# ====================================================================== #
# 跨空间：经验路由值拒绝，其余放行
# ====================================================================== #


def test_cross_space_experience_route_rejected(api) -> None:
    """路由值为 experience 的跨空间检索：参数错误（经验检索一次只查一个空间）。"""
    with pytest.raises(ValidationError, match="一个空间"):
        api.search(
            "启动服务",
            Context(
                scope=Scope(org=ORG),
                extensions={"spaces": [EXP_SPACE, PLAIN_SPACE], "memory_type": "experience"},
            ),
            security=internal_context(ScopedAuthenticator(ALICE)),
        )


def test_cross_space_skill_route_allowed(api) -> None:
    """技能检索侧放行：跨空间检索的路由值为 skill 不拒绝——技能条目按等值谓词
    可读，权限沿用空间规则（写入侧才整体拒绝，见 cloud_engine._require_profile）。
    """
    result = api.search(
        "查技能",
        Context(
            scope=Scope(org=ORG),
            extensions={"spaces": [EXP_SPACE, PLAIN_SPACE], "memory_type": "skill"},
        ),
        security=internal_context(ScopedAuthenticator(ALICE)),
    )
    assert result.items == [], "skill 无 profile 时按 fallback 检索，不报错"


def test_cross_space_without_route_value_allowed(api) -> None:
    """无路由值的跨空间检索照常：经验空间参与候选，经验条目被谓词排除在结果外。"""
    _write_experience_unit(api, "exp-cross-unit", "共享空间的部署经验")
    api.add(
        "偏好深色主题",
        PLAIN_SCOPE,
        security=internal_context(ScopedAuthenticator(ALICE)),
    )

    result = api.search(
        "主题",
        Context(scope=Scope(org=ORG), extensions={"spaces": [EXP_SPACE, PLAIN_SPACE]}),
        security=internal_context(ScopedAuthenticator(ALICE)),
        top_k=10,
    )

    contents = [item.content for item in result.items]
    assert contents == ["偏好深色主题"], "普通空间的条目照常返回"
    assert _PROBE.calls == [], "无路由值各空间走默认 profile"
