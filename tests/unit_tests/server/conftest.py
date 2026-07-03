# coding: utf-8
"""server UT fixtures：ASGI 直连 FastAPI app，mock 掉 memory_engine 隔离 LLM。

不走子进程 / 不占端口 / 不依赖 .env —— 直接用 httpx ASGITransport 打 app 对象。
LLM 调用全在 memory_engine 内部，故 mock 掉 engine 的方法即可彻底隔离模型，
端点层逻辑（请求校验、MemVariable→Param 适配、错误转 500、结果序列化、鉴权）才是 UT 被测对象。

默认无鉴权（MEMORY_API_KEY=""）；需要鉴权的用例用 auth_client fixture。
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from jiuwen_memory.server import memory_server


@pytest_asyncio.fixture
async def client(monkeypatch):
    """无鉴权的 ASGI 客户端。mock engine 方法供单测覆盖。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "", raising=True)
    transport = ASGITransport(app=memory_server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth_client(monkeypatch):
    """带鉴权的 ASGI 客户端：所有 POST 需携带正确 Bearer key。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "ut-secret-key", raising=True)
    transport = ASGITransport(app=memory_server.app)
    headers = {"Authorization": "Bearer ut-secret-key"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture
def mock_engine(mocker):
    """mock 掉全局 memory_engine 的所有端点用到的方法，返回 MagicMock。

    单测按需对某方法 .return_value / .side_effect 设定，断言端点对返回值的序列化与对异常的处理。
    默认返回 MagicMock，未显式配置的方法调用返回的是 MagicMock 对象（非 awaitable 会报错），
    故各用例需针对自己调的方法设置 AsyncMock 返回值。
    """
    engine = memory_server.memory_engine
    # 端点实际 await 的方法列表（端点层只调这些）
    methods = [
        "add_messages", "update_mem_by_id", "update_variables", "delete_variables",
        "delete_mem_by_scope", "get_variables", "search_user_mem",
        "search_user_history_summary", "get_user_mem_by_page",
    ]
    patches = {}
    for m in methods:
        patches[m] = mocker.patch.object(engine, m, new_callable=mocker.AsyncMock)
    yield engine


# ---------- 测试辅助：构造 engine 返回的对象 ----------
@pytest.fixture
def make_search_result(mocker):
    """构造 search_memory / search_user_history_summary 的 engine 返回项。

    engine 返回带 .mem_info(.mem_id/.content/.type) 和 .score 的对象；端点把它序列化成
    {mem_id, content, type, score}。type 是带 .value 的枚举。
    """
    def _make(mem_id: str, content: str, type_value: str = "semantic_memory", score: float = 0.9):
        result = mocker.Mock()
        result.mem_info.mem_id = mem_id
        result.mem_info.content = content
        result.mem_info.type = mocker.Mock()
        result.mem_info.type.value = type_value
        result.score = score
        return result
    return _make


@pytest.fixture
def make_mem_unit(mocker):
    """构造 get_user_mem_by_page 的 engine 返回项（扁平 .mem_id/.content/.type）。"""
    def _make(mem_id: str, content: str, type_value: str = "semantic_memory"):
        unit = mocker.Mock()
        unit.mem_id = mem_id
        unit.content = content
        unit.type = mocker.Mock()
        unit.type.value = type_value
        return unit
    return _make
