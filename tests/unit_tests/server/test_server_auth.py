# coding: utf-8
"""server UT —— 鉴权中间件 + GET 端点 + 启动安全守卫。

鉴权中间件在请求进端点前生效：GET 全放行、无 key 全放行、配 key 则 POST 需正确 Bearer。
启动守卫在 main()：绑定非本机地址且无 key → sys.exit(1)。
"""
from unittest.mock import MagicMock

import pytest

from jiuwen_memory.server import memory_server


# ==================== GET 端点（始终放行） ====================
@pytest.mark.asyncio
async def test_health_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert "message" in r.json()


@pytest.mark.asyncio
async def test_root_ok(client):
    r = await client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert "message" in body
    assert "endpoints" in body
    assert isinstance(body["endpoints"], list)
    assert "POST /add_messages/" in body["endpoints"]


@pytest.mark.asyncio
async def test_health_passes_even_with_key_set(auth_client):
    """配了 key，GET /health 仍放行（中间件对 GET 直接放行）。"""
    r = await auth_client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_root_passes_even_with_key_set(auth_client):
    r = await auth_client.get("/")
    assert r.status_code == 200


# ==================== 配 key 后 POST 鉴权 ====================
@pytest.mark.asyncio
async def test_auth_rejects_missing_bearer(auth_client, mock_engine):
    """无 Authorization header → 401。"""
    r = await auth_client.post("/get_variables/", json={"names": None},
                               headers={"Authorization": ""})
    assert r.status_code == 401
    assert "Unauthorized" in r.json()["detail"]
    mock_engine.get_variables.assert_not_called()


@pytest.mark.asyncio
async def test_auth_rejects_wrong_key(auth_client, mock_engine):
    """错 key → 401，不触达 engine。"""
    r = await auth_client.post("/get_variables/", json={"names": None},
                               headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 401
    mock_engine.get_variables.assert_not_called()


@pytest.mark.asyncio
async def test_auth_rejects_wrong_scheme(auth_client, mock_engine):
    """非 Bearer scheme（如 Basic）→ 401。"""
    r = await auth_client.post("/get_variables/", json={"names": None},
                               headers={"Authorization": "Basic ut-secret-key"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_rejects_bare_token(auth_client, mock_engine):
    """只有 token 无 'Bearer ' 前缀 → 401。"""
    r = await auth_client.post("/get_variables/", json={"names": None},
                               headers={"Authorization": "ut-secret-key"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_passes_correct_key(auth_client, mock_engine):
    """正确 Bearer key → 放行进端点。"""
    mock_engine.get_variables.return_value = {"k": "v"}
    r = await auth_client.post("/get_variables/", json={"names": None})
    assert r.status_code == 200
    assert r.json()["variables"] == {"k": "v"}


# ==================== 鉴权矩阵：所有写端点配 key 时无 Bearer 都 401 ====================
@pytest.mark.parametrize("path,body", [
    ("/add_messages/", {"messages": [{"role": "user", "content": "x"}]}),
    ("/update_mem_by_id/", {"mem_id": "m", "memory": "c"}),
    ("/update_variables/", {"variables": {}}),
    ("/delete_variables/", {"names": ["a"]}),
    ("/delete_mem_by_scope/", {"scope_id": "s"}),
    ("/get_variables/", {"names": None}),
    ("/search_memory/", {"query": "q"}),
    ("/search_user_history_summary/", {"query": "q"}),
    ("/get_user_mem_by_page/", {}),
])
@pytest.mark.asyncio
async def test_all_write_endpoints_require_auth(auth_client, path, body):
    """配置 key 后，任一 POST 端点无 Bearer 都应 401。"""
    r = await auth_client.post(path, json=body, headers={"Authorization": ""})
    assert r.status_code == 401


# ==================== 无 key 时全放行 ====================
@pytest.mark.asyncio
async def test_no_auth_passes_writes_when_key_unset(client, mock_engine):
    """未配置 key 时，无 header 的 POST 也放行（本地开发模式）。"""
    mock_engine.get_variables.return_value = {}
    r = await client.post("/get_variables/", json={"names": None})
    assert r.status_code == 200


# ==================== 启动安全守卫 ====================
# 守卫逻辑抽成纯函数 is_host_exposed_without_key，测试其返回值即可，
# 无需触发 main() 的 sys.exit（避免在测试中处理 SystemExit）。
def test_guard_flags_exposed_host_without_key(monkeypatch):
    """0.0.0.0 + 无 key → 守卫判定为应拒绝（True）。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "", raising=True)
    assert memory_server.is_host_exposed_without_key("0.0.0.0") is True


def test_guard_allows_localhost_without_key(monkeypatch):
    """127.0.0.1 + 无 key → 守卫放行（False），本机开发不需要 key。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "", raising=True)
    assert memory_server.is_host_exposed_without_key("127.0.0.1") is False


def test_guard_allows_localhost_name_without_key(monkeypatch):
    """localhost + 无 key → 守卫放行（False）。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "", raising=True)
    assert memory_server.is_host_exposed_without_key("localhost") is False


def test_guard_allows_exposed_host_with_key(monkeypatch):
    """0.0.0.0 + 有 key → 守卫放行（False），已配置鉴权可对外。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "some-key", raising=True)
    assert memory_server.is_host_exposed_without_key("0.0.0.0") is False


def test_guard_flags_other_exposed_hosts(monkeypatch):
    """任意非 127./localhost 的地址 + 无 key → 守卫拒绝。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "", raising=True)
    for host in ["0.0.0.0", "192.168.1.1", "10.0.0.1", "example.com"]:
        assert memory_server.is_host_exposed_without_key(host) is True, host


# ==================== main() 的 uvicorn 启动调用 ====================
def _patch_uvicorn(monkeypatch):
    """注入 fake uvicorn 模块到 sys.modules，返回 run 的 MagicMock。main() 内部 import 时取到此 fake。"""
    import sys
    import types
    fake_mod = types.ModuleType("uvicorn")
    fake_run = MagicMock()
    fake_mod.run = fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_mod)
    return fake_run


def test_main_invokes_uvicorn_for_localhost(monkeypatch):
    """127.0.0.1 + 无 key → main() 不退出，调用 uvicorn.run 启动。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "", raising=True)
    monkeypatch.setenv("IP", "127.0.0.1")
    monkeypatch.setenv("PORT", "8000")
    fake_run = _patch_uvicorn(monkeypatch)
    memory_server.main()
    fake_run.assert_called_once()
    _, kwargs = fake_run.call_args
    assert kwargs.get("host") == "127.0.0.1"
    assert kwargs.get("port") == 8000


def test_main_invokes_uvicorn_for_exposed_with_key(monkeypatch):
    """0.0.0.0 + 有 key → main() 不退出，调用 uvicorn.run 启动。"""
    monkeypatch.setattr(memory_server, "MEMORY_API_KEY", "some-key", raising=True)
    monkeypatch.setenv("IP", "0.0.0.0")
    monkeypatch.setenv("PORT", "9000")
    fake_run = _patch_uvicorn(monkeypatch)
    memory_server.main()
    fake_run.assert_called_once()
    _, kwargs = fake_run.call_args
    assert kwargs.get("host") == "0.0.0.0"
    assert kwargs.get("port") == 9000
