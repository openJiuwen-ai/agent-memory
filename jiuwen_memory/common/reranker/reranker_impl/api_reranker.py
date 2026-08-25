"""APIReranker — 云端 rerank API（多方言：Cohere 扁平风格 / 阿里 DashScope 信封）。

rerank 无统一标准，各厂商请求/响应形态不一。本实现用 ``dialect`` 旋钮覆盖主流两套：

- ``cohere``（智谱 / Jina / SiliconFlow / Cohere / voyage / TEM…）：扁平 body
  ``{model, query, documents, top_n}`` → 顶层 ``{results: [{index, relevance_score}]}``；
  端点 = ``base_url/rerank``。
- ``dashscope``（阿里百炼 ``gte-rerank*``）：信封 body
  ``{model, input:{query,documents}, parameters:{top_n,…}}`` → ``{output:{results:[…]}}``；
  端点 = ``base_url/services/rerank/text-rerank/text-rerank``。

两套的结果项都用 ``{index, relevance_score}``，故方言只差「端点 + body 拼装 + results
取法」三处，打分还原逻辑共用。换 ``dialect``/base_url/model 即可切厂商。rerank 无官方
SDK，故走 httpx——**懒加载**（未装时给明确提示），不在模块导入期连坐其余实现。

``model`` / ``api_key`` / ``base_url`` 优先经 ConfigSource 在每次 ``rerank`` 路径晚绑定
（S08）；构造参数为回落默认。注入的 ``client``（测试用）不会因 URL 变化而重建，
但仍使用晚绑定后的 URL / Bearer / model 发请求。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from jiuwen_memory.common._support import (
    outbound_verify,
    read_outbound_ssl,
    require_ca_file,
    require_https,
)
from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import BackendError, HealthCheckError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.reranker.base import Reranker, RerankerProducer

logger = get_logger(__name__)


# 每种方言 = 端点后缀 + body 拼装 + 从响应取 results 列表（项均为 {index, relevance_score}）。
_DIALECTS: Dict[str, Dict[str, Any]] = {
    "cohere": {
        "path": "/rerank",
        "body": lambda model, query, docs: {
            "model": model,
            "query": query,
            "documents": docs,
            "top_n": len(docs),
        },
        "results": lambda data: data.get("results", []),
    },
    "dashscope": {
        "path": "/services/rerank/text-rerank/text-rerank",
        "body": lambda model, query, docs: {
            "model": model,
            "input": {"query": query, "documents": docs},
            "parameters": {"return_documents": False, "top_n": len(docs)},
        },
        "results": lambda data: (data.get("output") or {}).get("results", []),
    },
}


class APIReranker(Reranker):
    """云端 rerank API（多方言）。对每条候选返回相关性分，**顺序与输入一致**。

    参数：
        model_name: rerank 模型名（智谱 ``"rerank"``、阿里 ``"gte-rerank-v2"`` 等）
        base_url: API 根地址（实现内部按方言拼端点后缀）
        api_key: API KEY（Bearer）
        timeout: 单次请求超时（秒）
        dialect: ``"cohere"`` | ``"dashscope"``
        client: 可注入的 httpx.Client（测试/复用连接用）；``None`` 则懒建
        config_source: 可选；每次 rerank 晚绑定 model/api_key/base_url
    """

    def __init__(
        self,
        model_name: str,
        base_url: str,
        api_key: str = "",
        timeout: float = 30.0,
        dialect: str = "cohere",
        client: object = None,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source=None,
        config_namespace: str = "reranker",
    ) -> None:
        """初始化 APIReranker。

        Args:
            model_name: 参数 model_name（str）。
            base_url: 参数 base_url（str）。
            api_key: 参数 api_key（str）。
            timeout: 参数 timeout（float）。
            dialect: 参数 dialect（str）。
            client: 参数 client（object）。
            ssl_verify: 参数 ssl_verify（bool）。
            ssl_ca_cert: 参数 ssl_ca_cert（str | None）。
            config_source: 参数 config_source。
            config_namespace: 参数 config_namespace（str）。

        Raises:
            ValidationError: 执行失败时抛出。
            ImportError: 执行失败时抛出。
        """
        dialect = (dialect or "cohere").lower()
        if dialect not in _DIALECTS:
            raise ValidationError(
                f"unknown rerank dialect: {dialect!r}（支持 {sorted(_DIALECTS)}）"
            )
        self._dialect = dialect
        self._spec = _DIALECTS[dialect]
        self._fallback_model = model_name
        self._fallback_base_url = base_url
        self._fallback_api_key = api_key
        self._timeout = timeout
        self._ssl_verify = ssl_verify
        self._ssl_ca_cert = ssl_ca_cert
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._injected_client = client is not None
        if client is not None:
            self._client = client
        else:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - 依赖缺失路径
                raise ImportError("APIReranker 需要 `pip install httpx`") from exc
            # ssl_verify 关闭时不干预（保持 httpx 默认）；开启时按 ssl_ca_cert 覆盖
            # 信任锚，缺证书回落系统 CA（公网端点走公共 CA，属正常状态）。
            client_kwargs: dict = {"timeout": timeout}
            if ssl_verify:
                client_kwargs["verify"] = outbound_verify(ssl_ca_cert)
            self._client = httpx.Client(**client_kwargs)

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``RERANKER``。"""
        return PluginType.RERANKER

    def health(self) -> None:
        """探活：调用一次 rerank 测试 API 可达。"""
        try:
            self.rerank("health check", ["ok"])
        except Exception as exc:
            raise HealthCheckError(f"Reranker health check failed: {exc}") from exc

    def rerank(self, query: str, texts: List[str]) -> List[float]:
        """对候选文本打相关性分，返回与 ``texts`` 等长、同序的分数列表。

        每次调用按 ConfigSource 解析 model/api_key/base_url，再按方言拼端点与 body。
        """
        if not texts:
            return []
        ep = self._endpoint()
        url = (ep.base_url or self._fallback_base_url).rstrip("/") + self._spec["path"]
        build_body: Callable = self._spec["body"]
        extract: Callable = self._spec["results"]
        try:
            resp = self._client.post(
                url,
                headers={"Authorization": f"Bearer {ep.api_key}"},
                json=build_body(ep.model, query, list(texts)),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("APIReranker(%s): rerank request failed — %s", self._dialect, exc)
            raise BackendError(f"rerank API call failed: {exc}") from exc

        # 响应按分排序、各带原始 index；据 index 还原到输入顺序（缺失者保持 0.0）。
        scores = [0.0] * len(texts)
        for item in extract(data):
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(scores):
                scores[idx] = float(item.get("relevance_score", 0.0))
        return scores

    def _endpoint(self):
        """解析当前应生效的 model / api_key / base_url（ConfigSource 优先）。"""
        from jiuwen_memory.config.binding import resolve_endpoint

        return resolve_endpoint(
            self._config_source,
            namespace=self._config_namespace,
            fallback_model=self._fallback_model,
            fallback_api_key=self._fallback_api_key,
            fallback_base_url=self._fallback_base_url,
        )


# -- 注册到 RerankerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@RerankerProducer.register("api")
def _build(config):
    """从装配 ComponentConfig 构造；注入内核共享的 default ConfigSource（若已装配）。"""
    base_url = config.get("reranker_base_url", "")
    ssl = read_outbound_ssl(config, "reranker")
    if ssl.verify:
        require_https(base_url, component="api reranker", param="reranker")
        require_ca_file(ssl.ca_cert, component="api reranker", param="reranker")
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    return APIReranker(
        model_name=config.get("reranker_model") or "rerank",
        base_url=base_url,
        api_key=config.get("reranker_api_key", ""),
        timeout=config.get("reranker_timeout", 30.0),
        dialect=config.get("reranker_dialect", "cohere"),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        config_source=ConfigSourceProducer.get_cached("default"),
    )
