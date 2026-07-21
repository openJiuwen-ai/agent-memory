# F04 — 权限上下文路由

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-17 |
| 影响范围 | `src/control/permission.py`、`src/control/types.py`、`src/control/permission_impl/`、`src/control/engine.py`、`src/api/memory_api_impl/local_memory_api.py`、`docs/specs/S03-memory-manage.md` |
| 测试基线 | `python3 -m compileall -q src/control src/api tests/unit/control/test_permission_context_routing.py tests/unit/api/test_build_kernel_config.py` 通过；`PYTHONPATH=src python3` 权限路由烟测通过；当前环境缺少 `pytest` / `ruff` 模块 |

## 背景

控制层已有 `PermissionManager.check(actor, target, action)`，只能表达 scope 和动作级别的权限。多记忆类型 pipeline 引入后，权限策略也需要按资源上下文区分，例如 coding 记忆要求更严格的同 owner 访问，而普通情景记忆可以使用较宽松的 grant 或 dev 策略。

仅靠调用方传 `memory_type` 不够安全：write/recall 的 `memory_type` 来自请求本身，可以直接用于入口鉴权；get/update/delete 作用于已有 unit，必须以真源中保存的 metadata 为准，不能信任调用方声明。

## 决策

新增 `PermissionContext`，并把 `PermissionManager.check` 扩展为兼容签名：

```python
def check(
    actor: Scope,
    target: Scope,
    action: Action,
    context: PermissionContext | None = None,
) -> bool:
    ...
```

`PermissionContext` 承载 `resource_type`、`memory_type`、`pipeline`、`unit_id`、`scope`、`tags`、`metadata`。旧实现可忽略 context，旧调用也可不传 context。

API 层的上下文来源：

1. `write`：从入参 `metadata["memory_type"]`、`metadata["pipeline"]`、`tags` 构造。
2. `recall`：从 `Context.extensions["memory_type"]` / `["pipeline"]` 构造，等值 filter 作为 memory_type 兜底。
3. `get/update`：先做基础 scope 门槛，再调用 `Engine.permission_context_for_unit` 从真源解析 unit 元数据，再做类型化权限检查。
4. `delete`：调用 `Engine.permission_contexts_for_delete` 解析 selector 命中的候选 unit，逐条做 DELETE 权限检查，全部通过后才执行删除。

新增 `permission_impl.routing_permission_manager`。它不自行定义授权语义，只按 `PermissionContext` 选择一个已配置的 `PermissionManager` delegate。`grant` / `revoke` 广播给全部 delegate，保证授权记录不会因为调用方不了解路由而落错后端。

## 拒绝的方案

拒绝让 `MemoryPipeline` 接管权限判断。权限是 API 层 PEP 的统一入口，交给 pipeline 会让审计、grant/revoke 和管理面权限语义分裂。

拒绝把 `memory_type` 作为 get/update/delete 的用户入参。已有 unit 的类型必须以真源 metadata 为准，否则调用方可以通过伪造 memory_type 绕过严格策略。

拒绝一次性新增完整 RBAC/ABAC 规则语言。当前需求只需要把资源上下文传给权限后端，并提供按上下文选择 delegate 的基础能力。

## 验证

新增 `tests/unit/control/test_permission_context_routing.py`：

- `write` 按请求 metadata 的 `memory_type` 路由。
- `recall` 按 `Context.extensions["memory_type"]` 路由。
- `get` 按真源中已保存的 unit `memory_type` 路由。
- `delete` 对 selector 命中的 unit 逐条按真源 `memory_type` 鉴权。

当前环境缺少 `pytest` / `ruff`，已用 `compileall` 和直接烟测验证关键路径。

## 已知遗留

- delete 按 selector 预解析候选上下文会扫描 KV；大规模后端需要更高效的 metadata-only 查询接口。
- audit/admin/job 权限仍走全局或 scope 级上下文，没有按 memory_type 分流。
- `routing` 权限后端的 route key 当前只支持 `memory_type`、`pipeline`、`resource_type` 或 metadata 字符串等值。
