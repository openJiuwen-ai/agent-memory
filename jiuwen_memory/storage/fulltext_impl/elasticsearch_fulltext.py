"""ElasticsearchFulltextStore — 基于 Elasticsearch 的全文倒排实现。

实现 :class:`~storage.fulltext.FulltextStore`：``insert/update/delete/get`` 走文档
CRUD，``search`` 走 ``match`` 关键词检索（BM25）。``scope`` 为方法显式入参——写入
时落到文档的 ``scope`` 嵌套字段，``search`` / 按 id 的 ``get`` / ``delete`` 物理
约束在该 scope 内（对非空维度施加 ``term`` 过滤），实现原生隔离（§5.2 / §7）；
``id`` 是 scope 内逻辑主键，物理 ``_id`` 由 scope+id 生成，``filters`` 仅承载 scope 之外的谓词。

``elasticsearch`` 客户端惰性导入与连接，未安装/未就绪不影响 ``import storage``。
目标客户端 API 为 elasticsearch-py 8.x。
"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
    ValidationError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterExpr,
    FilterLogic,
    FilterOp,
    Scope,
    filter_field_metadata_key,
)
from jiuwen_memory.storage.fulltext import FulltextProducer

from .._support import (
    read_ssl_config,
    require_tls_scheme,
    scope_dims,
    scope_segments,
    wrap_backend,
)
from ..base import StoreType
from ..fulltext import FulltextStore
from ..types import Document, ScoredID, TextQuery
from .retrieval_stopwords import RETRIEVAL_STOPWORDS

_RANGE_OPS = {FilterOp.GT: "gt", FilterOp.GTE: "gte", FilterOp.LT: "lt", FilterOp.LTE: "lte"}
_METADATA_ARRAY_FIELDS = "metadata_array_fields"


class ElasticsearchFulltextStore(FulltextStore):
    def __init__(
        self,
        *,
        hosts: list[str] | str | None = None,
        index: str = "agent_memory_fulltext",
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        text_field: str = "text",
        text_analyzer: str | None = None,
        refresh: str = "false",
        config_source=None,
        config_namespace: str = "fulltext_store",
        **options: Any,
    ) -> None:
        self._fallback_hosts = hosts or "http://localhost:9200"
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._index = index
        self._text_field = text_field
        # 建索引期分析器，按记忆主语言选（查询侧自动同规则）：
        # 英文 "english"（词干化+去停用词）；中文 "ik_max_word"（需 analysis-ik
        # 插件）或内置 "cjk"（二元切分兜底）；中英混合优先 ik_max_word。
        # None = ES standard（英文不词干化、中文单字切分）。仅在索引创建时
        # 生效——已存在的索引不会被重映射，变更后需删除或换 index 重建才能应用。
        self._text_analyzer = text_analyzer
        self._refresh = refresh  # "false" / "true" / "wait_for"
        self._auth = dict(username=username, password=password, api_key=api_key)
        self._options = options
        self._client: Any = None
        self._client_hosts: object | None = None

    @property
    def client(self) -> Any:
        hosts = self._resolved_hosts()
        fingerprint: object = (
            tuple(hosts) if isinstance(hosts, list) else hosts
        )
        if self._client is not None and self._client_hosts == fingerprint:
            return self._client
        try:
            from elasticsearch import Elasticsearch
        except ImportError as exc:
            raise BackendError(
                "elasticsearch client not installed (pip install elasticsearch)"
            ) from exc
        opts: dict[str, Any] = dict(self._options)
        if self._auth["api_key"]:
            opts["api_key"] = self._auth["api_key"]
        elif self._auth["username"]:
            opts["basic_auth"] = (self._auth["username"], self._auth["password"])
        with wrap_backend("elasticsearch connect"):
            self._client = Elasticsearch(hosts, **opts)
            self._client_hosts = fingerprint
            self._ensure_index()
        return self._client

    # --------------------------------------------------------------- 序列化
    @staticmethod
    def _scope_dict(scope: Scope) -> dict[str, str]:
        return {
            "org": scope.org,
            "space": scope.space,
            "user": scope.user,
            "agent": scope.agent,
            "session": scope.session,
        }

    @staticmethod
    def _doc_id(scope: Scope, logical_id: str) -> str:
        return ":".join((*scope_segments(scope), logical_id))

    @staticmethod
    def _logical_id(doc_id: str) -> str:
        parts = doc_id.split(":", 5)
        return parts[-1] if len(parts) == 6 else doc_id

    @staticmethod
    def _array_marker(key: str) -> dict[str, Any]:
        return {"term": {_METADATA_ARRAY_FIELDS: key}}

    @classmethod
    def _scalar_match(cls, key: str, query: dict[str, Any]) -> dict[str, Any]:
        return {
            "bool": {
                "filter": [query],
                "must_not": [cls._array_marker(key)],
            }
        }

    @classmethod
    def _filter_clause(cls, fc: FilterClause) -> dict[str, Any]:
        key = filter_field_metadata_key(fc.field)
        field = f"metadata.{key}"
        if fc.op == FilterOp.EQ:
            return cls._scalar_match(key, {"term": {field: fc.value}})
        if fc.op == FilterOp.NE:
            return {"bool": {"must_not": [cls._scalar_match(key, {"term": {field: fc.value}})]}}
        if fc.op == FilterOp.IN:
            return cls._scalar_match(key, {"terms": {field: fc.value}})
        if fc.op == FilterOp.NOT_IN:
            return {"bool": {"must_not": [cls._scalar_match(key, {"terms": {field: fc.value}})]}}
        if fc.op == FilterOp.CONTAINS:
            return {
                "bool": {
                    "filter": [
                        {"term": {field: fc.value}},
                        cls._array_marker(key),
                    ]
                }
            }
        if fc.op in _RANGE_OPS:
            # Lucene 的 range 对多值字段是「任一成员命中即匹配」，会让数组字段被范围
            # 谓词选中；真源复核与 pg 编译器都判否（pg 用 jsonb_typeof='number' 守卫）。
            # 此处同样限定标量，避免同一谓词在不同后端给出不同候选集。
            return cls._scalar_match(key, {"range": {field: {_RANGE_OPS[fc.op]: fc.value}}})
        raise ValidationError(f"unsupported filter op for fulltext: {fc.op}")

    @classmethod
    def _compile_filter(cls, expr: FilterExpr | None) -> dict[str, Any] | None:
        """把完整 FilterExpr 编译为 Elasticsearch bool/filter Query DSL。"""
        if expr is None:
            return None
        if isinstance(expr, FilterClause):
            return cls._filter_clause(expr)
        children = [cls._compile_filter(child) for child in expr.children]
        compiled = [child for child in children if child is not None]
        if expr.logic is FilterLogic.AND:
            return {"bool": {"filter": compiled}}
        if expr.logic is FilterLogic.OR:
            return {"bool": {"should": compiled, "minimum_should_match": 1}}
        if expr.logic is FilterLogic.NOT:
            return {"bool": {"must_not": compiled}}
        raise ValidationError(f"unsupported filter logic for fulltext: {expr.logic}")

    # --------------------------------------------------------------- CRUD
    def store_type(self) -> StoreType:
        return StoreType.FULLTEXT

    def health(self) -> None:
        try:
            ok = self.client.ping()
        except Exception as exc:
            raise HealthCheckError(f"elasticsearch ping failed: {exc}") from exc
        if not ok:
            raise HealthCheckError("elasticsearch ping returned falsy")

    def insert(self, scope: Scope, docs: list[Document]) -> None:
        if not docs:
            return
        ops: list[dict[str, Any]] = []
        for doc in docs:
            ops.append({"create": {"_index": self._index, "_id": self._doc_id(scope, doc.id)}})
            ops.append(self._source(scope, doc))
        with wrap_backend("elasticsearch insert"):
            resp = self.client.bulk(operations=ops, refresh=self._refresh)
        if resp.get("errors"):
            for item in resp["items"]:
                res = item.get("create", {})
                status = res.get("status", 0)
                if status == 409:
                    raise ConflictError(entity="document", key=res.get("_id", ""))
                if status >= 400:
                    raise BackendError(f"elasticsearch insert failed: {res.get('error')}")

    def update(self, scope: Scope, docs: list[Document]) -> None:
        if not docs:
            return
        missing = self._missing_ids(scope, [doc.id for doc in docs])
        if missing:
            raise NotFoundError(entity="document", key=missing[0])
        ops: list[dict[str, Any]] = []
        for doc in docs:
            ops.append({"index": {"_index": self._index, "_id": self._doc_id(scope, doc.id)}})
            ops.append(self._source(scope, doc))
        with wrap_backend("elasticsearch update"):
            resp = self.client.bulk(operations=ops, refresh=self._refresh)
        if resp.get("errors"):
            raise BackendError(f"elasticsearch update failed: {resp['items']}")

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if not ids:
            return
        # delete_by_query 受 scope 约束：只删 scope 内命中的 id（幂等）。
        query = {
            "bool": {
                "filter": [
                    {"ids": {"values": [self._doc_id(scope, doc_id) for doc_id in ids]}},
                    *self._scope_filters(scope),
                ]
            }
        }
        with wrap_backend("elasticsearch delete"):
            self.client.delete_by_query(
                index=self._index, query=query, refresh=bool(self._refresh != "false")
            )

    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        if not ids:
            return []
        with wrap_backend("elasticsearch get"):
            resp = self.client.mget(
                index=self._index,
                ids=[self._doc_id(scope, doc_id) for doc_id in ids],
            )
        wanted = dict(scope_dims(scope))
        out: list[Document] = []
        for d in resp["docs"]:
            if not d.get("found"):
                continue
            src = d["_source"]
            stored = src.get("scope") or {}
            if all(stored.get(dim) == val for dim, val in wanted.items()):  # scope 内才返回
                out.append(self._to_document(d["_id"], src))
        return out

    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        tokens = self._analyze_query(query.text)
        if not tokens:
            return []
        filters = self._scope_filters(scope)
        compiled = self._compile_filter(query.filters)
        if compiled is not None:
            filters.append(compiled)
        keyword_query = {
            "bool": {
                "should": [
                    {"term": {self._text_field: token}}
                    for token in tokens
                ],
                "minimum_should_match": 1,
            }
        }
        bool_query = {"must": [keyword_query], "filter": filters}
        with wrap_backend("elasticsearch search"):
            resp = self.client.search(
                index=self._index, query={"bool": bool_query}, size=query.top_k
            )
        results: list[ScoredID] = []
        for hit in resp["hits"]["hits"]:
            source = hit.get("_source") or {}
            logical_id = source.get("logical_id") or self._logical_id(hit["_id"])
            results.append(
                ScoredID(
                    id=logical_id,
                    score=float(hit["_score"]),
                )
            )
        return results

    def _resolved_hosts(self) -> list[str] | str:
        """当前 ES hosts（ConfigSource ``fulltext_store.hosts``）。"""
        from jiuwen_memory.config.binding import resolve_connection_url

        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="hosts",
            fallback=(
                self._fallback_hosts
                if isinstance(self._fallback_hosts, str)
                else ",".join(self._fallback_hosts)
            ),
        )
        if live is None:
            return self._fallback_hosts
        # 投影多为逗号分隔串；单 host 保持 str，多 host 拆成 list 供 ES 客户端
        parts = [p.strip() for p in str(live).split(",") if p.strip()]
        if len(parts) <= 1:
            return parts[0] if parts else self._fallback_hosts
        return parts

    def _ensure_index(self) -> None:
        if not self._client.indices.exists(index=self._index):
            self._client.indices.create(
                index=self._index,
                mappings={
                    # 元数据里的字符串一律映射为 keyword：等值/集合/包含过滤需要精确
                    # 匹配（text 的分析器会拆词、小写化，导致 "Red Hat" 之类匹配不上）。
                    #
                    # 整数与浮点数一律映射为 double。metadata 值是 JSON 原生标量，
                    # mapping 由该字段第一条文档决定：不接管 long 的话，先写 8 会把
                    # priority 定成 long，此后 9.5 在索引里被截断成 9——_source 仍显示
                    # 9.5，range gte 9.5 却查不出这条文档。接管 double 则是因为 ES 对
                    # JSON 浮点默认推断 float(32 位，尾数 24 bit)，2^24 以上的整数会
                    # 塌陷——16777216 与 16777217 索引成同一个值，两个 EQ 互相命中。
                    # 布尔仍按动态推断。
                    "dynamic_templates": [
                        {
                            "metadata_strings_as_keyword": {
                                "path_match": "metadata.*",
                                "match_mapping_type": "string",
                                "mapping": {"type": "keyword"},
                            }
                        },
                        {
                            "metadata_longs_as_double": {
                                "path_match": "metadata.*",
                                "match_mapping_type": "long",
                                "mapping": {"type": "double"},
                            }
                        },
                        {
                            "metadata_floats_as_double": {
                                "path_match": "metadata.*",
                                "match_mapping_type": "double",
                                "mapping": {"type": "double"},
                            }
                        },
                    ],
                    "properties": {
                        self._text_field: (
                            {"type": "text", "analyzer": self._text_analyzer}
                            if self._text_analyzer
                            else {"type": "text"}
                        ),
                        "logical_id": {"type": "keyword"},
                        "scope": {
                            "properties": {
                                "org": {"type": "keyword"},
                                "space": {"type": "keyword"},
                                "user": {"type": "keyword"},
                                "agent": {"type": "keyword"},
                                "session": {"type": "keyword"},
                            }
                        },
                        "metadata": {"type": "object"},
                        # ES 的倒排字段不区分单值与数组。记录数组 key，供 EQ 与
                        # CONTAINS 编译器恢复公共过滤契约里的形态语义；该字段不暴露
                        # 给 Document。
                        _METADATA_ARRAY_FIELDS: {"type": "keyword"},
                    },
                },
            )
            return
        # mapping 可原地增加，但历史文档没有派生标记，仍需重建索引后才能获得
        # 严格的 EQ / CONTAINS 语义。
        self._client.indices.put_mapping(
            index=self._index,
            properties={_METADATA_ARRAY_FIELDS: {"type": "keyword"}},
        )

    def _source(self, scope: Scope, doc: Document) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        for key, value in doc.metadata.items():
            namespace, separator, nested_key = key.partition(".")
            if separator and namespace in {"system_metadata", "user_metadata"}:
                metadata.setdefault(namespace, {})[nested_key] = value
            else:
                metadata[key] = value
        return {
            "logical_id": doc.id,
            self._text_field: doc.text,
            "scope": self._scope_dict(scope),
            "metadata": metadata,
            _METADATA_ARRAY_FIELDS: [
                key for key, value in doc.metadata.items() if isinstance(value, list)
            ],
        }

    def _to_document(self, doc_id: str, src: dict[str, Any]) -> Document:
        metadata: dict[str, Any] = {}
        for key, value in (src.get("metadata") or {}).items():
            if key in {"system_metadata", "user_metadata"} and isinstance(value, dict):
                metadata.update(
                    {
                        f"{key}.{nested_key}": nested_value
                        for nested_key, nested_value in value.items()
                    }
                )
            else:
                metadata[key] = value
        return Document(
            id=src.get("logical_id") or self._logical_id(doc_id),
            text=src.get(self._text_field, ""),
            metadata=metadata,
        )

    def _scope_filters(self, scope: Scope) -> list[dict[str, Any]]:
        return [{"term": {f"scope.{dim}": val}} for dim, val in scope_dims(scope)]

    def _missing_ids(self, scope: Scope, ids: list[str]) -> list[str]:
        physical_to_logical = {self._doc_id(scope, doc_id): doc_id for doc_id in ids}
        with wrap_backend("elasticsearch mget"):
            resp = self.client.mget(index=self._index, ids=list(physical_to_logical))
        return [physical_to_logical[d["_id"]] for d in resp["docs"] if not d.get("found")]

    def _analyze_query(self, text: str) -> list[str]:
        """用索引字段的实际 analyzer 分词，并过滤内部中文停用词。"""
        with wrap_backend("elasticsearch analyze query"):
            response = self.client.indices.analyze(
                index=self._index,
                field=self._text_field,
                text=text,
            )
        tokens = (
            str(item.get("token", "")).strip()
            for item in response.get("tokens", [])
        )
        return list(
            dict.fromkeys(
                token
                for token in tokens
                if token and token not in RETRIEVAL_STOPWORDS
            )
        )


# -- 注册到 FulltextProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FulltextProducer.register("elasticsearch")
def _build(config):
    # 三方库后端：hosts 必填，未配置即在 build 阶段报错；其余构造参数有默认值，可经 params 覆盖。
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    hosts = Factory.require_param(config, "hosts", backend="elasticsearch fulltext")
    ssl = read_ssl_config(config, backend="elasticsearch fulltext")
    options: dict[str, Any] = {}
    if ssl.verify:
        # hosts 只承载地址：elasticsearch-py 解析 URL 时不读 query，证书只能走构造参数。
        require_tls_scheme(
            hosts,
            expected="https",
            component="elasticsearch fulltext",
            param="params.hosts",
        )
        options["ca_certs"] = ssl.ca_cert
    return ElasticsearchFulltextStore(
        hosts=hosts,
        index=Factory.cfg_get(config, "index", "agent_memory_fulltext"),
        username=Factory.cfg_get(config, "username"),
        password=Factory.cfg_get(config, "password"),
        api_key=Factory.cfg_get(config, "api_key"),
        text_field=Factory.cfg_get(config, "text_field", "text"),
        text_analyzer=Factory.cfg_get(config, "text_analyzer"),
        refresh=Factory.cfg_get(config, "refresh", "false"),
        config_source=ConfigSourceProducer.get_cached("default"),
        **options,
    )
