# F04 — 权限上下文路由

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27 |
| 影响范围 | `src/control/permission.py`、`src/control/types.py`、`src/control/permission_impl/`、`src/control/engine.py`、`src/api/memory_api_impl/local_memory_api.py`、`docs/specs/S03-control.md` |
| 测试基线 | `PYTHONPATH=src uv run --no-sync pytest -q tests/unit/control/test_permission_context_routing.py tests/unit/control/test_pipeline.py tests/unit/api/test_handler_identity_split.py tests/unit/api/test_write_reserved_metadata.py` 通过；本特性变更的 Python 文件通过 `ruff check` |

## 背景

控制层已有 `PermissionManager.check(actor, target, action)`，只能表达 scope 和动作级别的权限。多记忆类型 pipeline 引入后，权限策略也需要按资源上下文区分，例如 coding 记忆要求更严格的同 owner 访问，而普通情景记忆可以使用较宽松的 grant 或 dev 策略。

仅靠调用方传 `memory_type` 不够安全：add/search 的 `memory_type` 来自请求本身，可以直接用于入口鉴权；get/update/delete 作用于已有 unit，必须以真源中保存的 metadata 为准，不能信任调用方声明。

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

1. `add`：从入参 `metadata["memory_type"]`、`metadata["pipeline"]`、`tags` 构造；
   业务 metadata 在真源中保留原生类型，进入 PermissionContext 的路由值才规范为字符串。
2. `search`：先从规范化 `FilterExpr` 提取逻辑上强制的唯一等值，再由
   `Context.extensions[route_key]` 的非空值覆盖。OR 多值、NOT、AND 冲突不产生路由值。
   该取值规则与执行侧 `MemoryPipeline` 一致。
3. `list`：从入参 `memory_types` 构造；显式传多个类型时逐个 memory_type 做 READ 权限检查，全部通过后才枚举。
4. `get/update`：先做基础 scope 门槛，再调用 `Engine.permission_context_for_unit` 从真源解析 unit 元数据，再做类型化权限检查。
5. `delete`：调用 `Engine.permission_contexts_for_delete` 解析 selector 命中的候选 unit，逐条做 DELETE 权限检查，全部通过后才执行删除。

新增 `permission_impl.routing_permission_manager`。它不自行定义授权语义，只按
`PermissionContext` 选择一个已配置的 `PermissionManager` delegate。授权路由只接受
`routes` 中显式声明的业务值；直接传 policy 名或未知值均落 `fallback`，避免调用方
自行挑选审查策略。`fallback` 承接路由值缺失的请求，装配期禁止配置为
`allow_all`，必须是最小权限策略。`grant` / `revoke` 广播给全部 delegate。

对 search，仅选对 delegate 仍不足以防越权：API 会通过
`PermissionManager.routing_fields()` 获取授权所依据的字段，把该路由值作为系统
`FilterClause(EQ)` 回注 `RetrievalQuery.filters`，并与用户表达式做外层 `AND`。
因此按 `memory_type=episodic` 获得的授权只能读取同类型数据，不能再用另一组 filters
指向 `coding` 记忆。

## 拒绝的方案

拒绝让 `MemoryPipeline` 接管权限判断。权限是 API 层 PEP 的统一入口，交给 pipeline 会让审计、grant/revoke 和管理面权限语义分裂。

拒绝把 `memory_type` 作为 get/update/delete 的用户入参。已有 unit 的类型必须以真源 metadata 为准，否则调用方可以通过伪造 memory_type 绕过严格策略。

拒绝一次性新增完整 RBAC/ABAC 规则语言。当前需求只需要把资源上下文传给权限后端，并提供按上下文选择 delegate 的基础能力。

## 验证

新增 `tests/unit/control/test_permission_context_routing.py`：

- `add` 按请求 metadata 的 `memory_type` 路由。
- `search` 的 extensions 与等值 filter 使用同一优先级解析，并把授权路由值回注数据过滤。
- `list` 按请求 `memory_types` 路由，显式多类型请求逐类型鉴权。
- 未声明、未知路由值和直接 policy 名均落最小权限 fallback。
- `allow_all` 作为 routing fallback 在装配期失败。
- OR/NOT/冲突 filter 不会被误判为强制路由等值。
- `get` 按真源中已保存的 unit `memory_type` 路由。
- `delete` 对 selector 命中的 unit 逐条按真源 `memory_type` 鉴权。

## 已知遗留

- delete 按 selector 预解析候选上下文会扫描 KV；大规模后端需要更高效的 metadata-only 查询接口。
- audit/admin/job 权限仍走全局或 scope 级上下文，没有按 memory_type 分流。
- `routing` 权限后端的 route key 当前只支持 `memory_type`、`pipeline`、
  `resource_type` 或可规范为字符串的 metadata 等值。
