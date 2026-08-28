# coding: utf-8
"""server UT —— 检索端点 /search_memory/ /search_user_history_summary/ /get_user_mem_by_page/。

被测对象：query/num/threshold/page 透传、engine 返回对象序列化（mem_info/score 或扁平 unit）、
memory_type 字符串→枚举转换、异常转 500。engine 全 AsyncMock。
"""
import pytest

from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType


# ==================== /search_memory/ ====================
def _search_body(query="饮品", **extra):
    p = {"query": query, "num": 10, "user_id": "u1", "scope_id": "s1", "threshold": 0.3}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_search_memory_single_result(client, mock_engine, make_search_result):
    """单条结果序列化为 {mem_id,content,type,score}。"""
    mock_engine.search_user_mem.return_value = [make_search_result("m1", "爱吃茶", "semantic_memory", 0.9)]
    r = await client.post("/search_memory/", json=_search_body())
    assert r.status_code == 200
    res = r.json()["results"]
    assert len(res) == 1
    assert res[0] == {"mem_id": "m1", "content": "爱吃茶", "type": "semantic_memory", "score": 0.9}


@pytest.mark.asyncio
async def test_search_memory_multiple_results_order_preserved(client, mock_engine, make_search_result):
    """多条结果顺序保留。"""
    mock_engine.search_user_mem.return_value = [
        make_search_result("m1", "a"), make_search_result("m2", "b"), make_search_result("m3", "c"),
    ]
    r = await client.post("/search_memory/", json=_search_body())
    res = r.json()["results"]
    assert [x["mem_id"] for x in res] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_search_memory_empty_results(client, mock_engine):
    """空结果 → {"results": []}。"""
    mock_engine.search_user_mem.return_value = []
    r = await client.post("/search_memory/", json=_search_body())
    assert r.status_code == 200
    assert r.json()["results"] == []


@pytest.mark.asyncio
async def test_search_memory_passthrough(client, mock_engine, make_search_result):
    """query/num/user/scope/threshold 透传。"""
    mock_engine.search_user_mem.return_value = [make_search_result("m1", "c")]
    await client.post("/search_memory/", json=_search_body(
        query="咖啡", num=5, user_id="u9", scope_id="s9", threshold=0.5))
    call = mock_engine.search_user_mem.call_args
    assert call.kwargs["query"] == "咖啡"
    assert call.kwargs["num"] == 5
    assert call.kwargs["user_id"] == "u9"
    assert call.kwargs["scope_id"] == "s9"
    assert call.kwargs["threshold"] == 0.5


@pytest.mark.asyncio
async def test_search_memory_default_threshold(client, mock_engine, make_search_result):
    """不传 threshold 默认 0.3。"""
    mock_engine.search_user_mem.return_value = []
    body = _search_body()
    del body["threshold"]
    await client.post("/search_memory/", json=body)
    assert mock_engine.search_user_mem.call_args.kwargs["threshold"] == 0.3


@pytest.mark.asyncio
async def test_search_memory_missing_query_rejected(client, mock_engine):
    r = await client.post("/search_memory/", json={"num": 10})
    assert r.status_code == 422
    mock_engine.search_user_mem.assert_not_called()


@pytest.mark.asyncio
async def test_search_memory_engine_error_500(client, mock_engine):
    mock_engine.search_user_mem.side_effect = RuntimeError("search boom")
    r = await client.post("/search_memory/", json=_search_body())
    assert r.status_code == 500
    assert "search boom" in r.json()["detail"]


# ==================== /search_user_history_summary/ ====================
@pytest.mark.asyncio
async def test_search_summary_serializes(client, mock_engine, make_search_result):
    """summary 端点与 search_memory 共享序列化逻辑。"""
    mock_engine.search_user_history_summary.return_value = [
        make_search_result("s1", "用户喜欢喝茶", "summary", 0.85),
    ]
    r = await client.post("/search_user_history_summary/", json=_search_body(query="偏好"))
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[0] == {"mem_id": "s1", "content": "用户喜欢喝茶", "type": "summary", "score": 0.85}


@pytest.mark.asyncio
async def test_search_summary_empty(client, mock_engine):
    mock_engine.search_user_history_summary.return_value = []
    r = await client.post("/search_user_history_summary/", json=_search_body())
    assert r.status_code == 200
    assert r.json()["results"] == []


@pytest.mark.asyncio
async def test_search_summary_passthrough(client, mock_engine, make_search_result):
    mock_engine.search_user_history_summary.return_value = []
    await client.post("/search_user_history_summary/", json=_search_body(num=3, threshold=0.4))
    call = mock_engine.search_user_history_summary.call_args
    assert call.kwargs["num"] == 3
    assert call.kwargs["threshold"] == 0.4


@pytest.mark.asyncio
async def test_search_summary_engine_error_500(client, mock_engine):
    mock_engine.search_user_history_summary.side_effect = RuntimeError("sum boom")
    r = await client.post("/search_user_history_summary/", json=_search_body())
    assert r.status_code == 500


# ==================== /get_user_mem_by_page/ ====================
def _page_body(**extra):
    p = {"user_id": "u1", "scope_id": "s1", "page_size": 10, "page_idx": 1, "memory_type": "unknown"}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_get_page_empty(client, mock_engine):
    mock_engine.get_user_mem_by_page.return_value = []
    r = await client.post("/get_user_mem_by_page/", json=_page_body())
    assert r.status_code == 200
    assert r.json() == {"results": [], "total": 0}


@pytest.mark.asyncio
async def test_get_page_serializes_units(client, mock_engine, make_mem_unit):
    """扁平 unit（.mem_id/.content/.type）序列化，total = len。"""
    mock_engine.get_user_mem_by_page.return_value = [
        make_mem_unit("m1", "内容A", "semantic_memory"),
        make_mem_unit("m2", "内容B", "episodic_memory"),
    ]
    r = await client.post("/get_user_mem_by_page/", json=_page_body())
    res = r.json()
    assert res["total"] == 2
    item0 = res["results"][0]
    assert item0["mem_id"] == "m1"
    assert item0["content"] == "内容A"
    assert item0["type"] == "semantic_memory"
    assert "timestamp" in item0
    assert "source_id" in item0
    assert res["results"][1]["type"] == "episodic_memory"


@pytest.mark.asyncio
async def test_get_page_memory_type_to_enum(client, mock_engine):
    """memory_type 字符串经 get_memory_type_enum() 转成 MemoryType 枚举传给 engine。"""
    mock_engine.get_user_mem_by_page.return_value = []
    await client.post("/get_user_mem_by_page/", json=_page_body(memory_type="semantic_memory"))
    assert mock_engine.get_user_mem_by_page.call_args.kwargs["memory_type"] == MemoryType.SEMANTIC_MEMORY


@pytest.mark.parametrize("type_str,expected", [
    ("user_profile", MemoryType.USER_PROFILE),
    ("semantic_memory", MemoryType.SEMANTIC_MEMORY),
    ("episodic_memory", MemoryType.EPISODIC_MEMORY),
    ("variable", MemoryType.VARIABLE),
    ("summary", MemoryType.SUMMARY),
    ("middle_term_memory", MemoryType.MIDDLE_TERM_MEMORY),
    ("unknown", MemoryType.UNKNOWN),
])
@pytest.mark.asyncio
async def test_get_page_all_memory_types(client, mock_engine, type_str, expected):
    """全部 MemoryType 枚举值都能从字符串正确转换。"""
    mock_engine.get_user_mem_by_page.return_value = []
    await client.post("/get_user_mem_by_page/", json=_page_body(memory_type=type_str))
    assert mock_engine.get_user_mem_by_page.call_args.kwargs["memory_type"] == expected


@pytest.mark.asyncio
async def test_get_page_invalid_memory_type_fallback_unknown(client, mock_engine):
    """非法 memory_type 字符串兜底为 UNKNOWN，不报错。"""
    mock_engine.get_user_mem_by_page.return_value = []
    await client.post("/get_user_mem_by_page/", json=_page_body(memory_type="nonsense"))
    assert mock_engine.get_user_mem_by_page.call_args.kwargs["memory_type"] == MemoryType.UNKNOWN


@pytest.mark.asyncio
async def test_get_page_memory_type_case_insensitive(client, mock_engine):
    """memory_type 大小写不敏感（端点 .lower() 后转枚举）。"""
    mock_engine.get_user_mem_by_page.return_value = []
    await client.post("/get_user_mem_by_page/", json=_page_body(memory_type="SUMMARY"))
    assert mock_engine.get_user_mem_by_page.call_args.kwargs["memory_type"] == MemoryType.SUMMARY


@pytest.mark.asyncio
async def test_get_page_passthrough_pagination(client, mock_engine, make_mem_unit):
    """page_size/page_idx/user/scope 透传。"""
    mock_engine.get_user_mem_by_page.return_value = [make_mem_unit("m1", "c")]
    await client.post("/get_user_mem_by_page/", json=_page_body(
        user_id="u9", scope_id="s9", page_size=20, page_idx=2))
    call = mock_engine.get_user_mem_by_page.call_args
    assert call.kwargs["user_id"] == "u9"
    assert call.kwargs["scope_id"] == "s9"
    assert call.kwargs["page_size"] == 20
    assert call.kwargs["page_idx"] == 2


@pytest.mark.asyncio
async def test_get_page_engine_error_500(client, mock_engine):
    mock_engine.get_user_mem_by_page.side_effect = RuntimeError("page boom")
    r = await client.post("/get_user_mem_by_page/", json=_page_body())
    assert r.status_code == 500
