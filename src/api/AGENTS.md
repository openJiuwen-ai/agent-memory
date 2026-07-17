# Agent Memory API（接口层）

**规约文档**：[S02-memory-api.md](../../docs/specs/S02-memory-api.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

统一对外 Core API，所有接入形态（SDK/CLI/MCP/HTTP）最终映射到 `MemoryAPI`。本层是控制层的薄封装：做参数装配与鉴权，编排逻辑全部在 `src/control`。

## 模块地图

| 文件 | 职责 |
|---|---|
| `memory_api.py` | MemoryAPI 抽象接口：统一语义定义（write/recall/get/update/delete/evolve/admin/inspect/trace/audit/grant/revoke） |
| `memory_api_impl/` | 具体实现目录 |
| `memory_api_impl/assembly.py` | 装配入口：`build_kernel(config)` 递归构建 MemoryAPI 实例 |
| `memory_api_impl/local_memory_api.py` | LocalMemoryAPI：委托 Engine/Governor/Scheduler/PermissionManager + PEP 鉴权 |

## 行为铁律

1. **本层不做编排**  
   `MemoryAPI` 只做三件事：鉴权（PEP）、参数装配、委托。编排逻辑（write 路径、recall 路径、evolve 调度）全部在 `control/MemoryEngine`，禁止在本层堆业务逻辑。

2. **identity 不下沉**  
   鉴权通过后只透传已鉴权的 target `scope`，`identity` 参数不传入控制层/检索层/构建层/存储层。

3. **recall 参数拆分在本层边界**  
   `recall(query, context, *, identity, ...)` 中的 `context: Context` 在本层拆开：
   - `context.scope` 作独立轴穿透到 Engine
   - `context.extensions["max_tokens"]` 由 API 边界解析为 `RetrievalQuery.max_tokens`
   - 其余 `context.extensions` 写入 `RetrievalQuery.extensions`

4. **admin_* 不经 Engine**  
   `admin_get/set/all` 直达 `PolicyManager`，不经过 `MemoryEngine`（Engine 中对应方法抛 NotImplementedError）。

5. **write/write_async 分离**  
   `write` 是同步桥接（内部 `asyncio.run(write_async)`），供 CLI/脚本使用；`write_async` 直通 Engine 协程，供事件循环形态使用。

## PEP 鉴权流程

```
MemoryAPI.method(scope=target, identity=caller)
  → 构造 PermissionContext（write/recall 来自入参；get/update/delete 来自 Engine 元数据解析）
  → PermissionManager.check(actor=identity, target=scope, action=<对应动作>, context=...)
    → 通过 → 委托 Engine/Governor/PolicyManager（仅传 scope，不传 identity）
    → 拒绝 → 抛 PermissionDeniedError
  → 落审计事件（含 identity + action + target_id + 时间）
```

## 与其他子目录的边界

**本模块管**：
- 统一对外接口定义（语义一致性）
- 鉴权执行（PEP）与入口审计
- 参数装配（context 拆分、RetrievalQuery 组装）
- 同步/异步桥接

**不管**：
- 编排逻辑（归 `control/MemoryEngine`）
- 记忆写入/落盘（归 `construction`）
- 检索链路（归 `retrieval`）
- 存储操作（归 `storage`）
- 策略存储（PolicyManager 实现在 `control`）

## 本地约束

1. `identity` 为必填 keyword-only 参数，与 `scope` 同为 Scope 类型，强制具名传入防止位置传反。
2. 所有数据面方法（write/recall/get/update/delete/evolve）都需要鉴权，治理面（inspect/trace/audit）也需要鉴权。
3. 装配由 `assembly.build_kernel(config)` 完成，递归调用各 Producer.create_from(spec)。
4. 实现类（LocalMemoryAPI）不对外暴露，外部只依赖 `MemoryAPI` 抽象接口。
