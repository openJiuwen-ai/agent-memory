# F11 — batch_update 批量更新接口设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-05 |
| 影响范围 | jiuwen_memory/api/，jiuwen_memory/control/，jiuwen_memory_entry/mcp_server/，jiuwen_memory_entry/http_server/，docs/specs/S02-memory-api.md，docs/specs/S03-control.md，docs/design/architecture.md，docs/features/F03-mcp-surface.md |
| 测试基线 | `tests/unit/api/test_batch_update.py`（12 passed）、`tests/integration/jiuwen_memory_entry/test_http_batch_update.py`（5 passed） |
| Refs | — |

> 本文归档 `batch_update` 的第一版设计。目标是提供批量修正入口，
> 但不改变单条 `update` 的版本语义（SUPERSEDE/OVERWRITE）、鉴权粒度与审计口径。

---

## 背景

`MemoryAPI.update` 一次只修正一条记忆单元。调用方做批量重打标签、批量降权、
批量修改 metadata 或离线修正时，只能在外层自行循环调用：

```python
for unit_id in unit_ids:
    api.update(unit_id, scope, patch, security=...)
```

与 `batch_add` 解决的问题类似（见 F03），这带来三个痛点：

1. **调用方重复实现错误处理和结果对齐**：每条 `update` 可能因 FORGOTTEN 状态、
   鉴权失败或存储异常而失败。外层循环需要自己维护 index、成功/失败归因。
2. **selector 级批量语义缺失**：实际场景常常是「把某 scope 下所有带 `tag:old`
   的记忆统一改成 `tag:new`」，调用方需要先 `list` 再逐条 `update`，两次往返
   之间数据可能变化，且无法在引擎内做 post-match 截断。
3. **统一 patch 与逐条 patch 无法区分**：有时所有候选应用同一个 patch（批量改标签），
   有时每条候选需要不同的 patch（逐条改 content）。循环调用无法在接口层表达这一语义。

因此新增批量更新接口，设计原则与 F03 `batch_add` 保持一致：batch 是调用语义，
第一版内部默认串行保序，不默认并发。

## 决策

### 1. 新增 UpdateSelector / BatchUpdateOutcome / BatchUpdateResult

新增公共数据结构，类型落在 `control/types.py`，由 `api` 和 `control` 共同引用。

```python
@dataclass
class UpdateSelector:
    """batch_update 的目标选择：各条件取「与」，至少给出一项。"""
    unit_ids: list[str] = field(default_factory=list)
    scope: Scope | None = None
    tags: list[str] = field(default_factory=list)
    before: datetime | None = None
    after: datetime | None = None
    filters: FilterExpr | None = None
    limit: int | None = None  # post-match 截断


@dataclass
class BatchUpdateOutcome:
    """批量更新的逐项结果。"""
    index: int
    unit_id: str
    updated_unit: MemoryUnit | None = None
    error: str = ""
    error_type: str = ""


@dataclass
class BatchUpdateResult:
    """与候选顺序对齐的批量更新结果。"""
    outcomes: list[BatchUpdateOutcome] = field(default_factory=list)
```

**与 `DeleteSelector` 的关系**：`UpdateSelector` 与 `DeleteSelector` 结构相同
（`unit_ids` / `scope` / `tags` / `before` / `after` / `filters`），但**不携带 `mode`**——
批量更新的版本语义由各 `MemoryPatch.mode` 逐条决定（SUPERSEDE 或 OVERWRITE），
而非 selector 级统一。

**`after` 字段**：`DeleteSelector` 没有 `after`，`UpdateSelector` 新增 `after` 支持
「只更新某时间段之后的消息」场景。`before` / `after` 均比较 `t_message`，与 delete 的
`before` 比较 `t_message` 对齐。

**`limit` 字段**：显式契约，取值必须为正整数（`0` / 负数抛 `ValidationError`，不静默忽略
——静默忽略会把「更新全部候选」当宽放行为，调用方拿不到任何信号）。统一 patch 模式做
post-match 截断（全量匹配后取前 N 条执行更新，「最多更新 N 条」）；逐条 patch 模式只做
下界承诺（不小于 `len(patches)`，否则抛 `ValidationError`），**不截断候选集**——截断会让
`unit_ids` 后段查不到候选被误判 `NotFoundError`。API 层另接受 `limit` 参数覆盖
selector 内的 `limit`，两者统一走同一校验。

### 2. 双模式：统一 patch 与逐条 patch

`patches` 列表长度决定模式：

| 模式 | 条件 | 语义 | 校验 |
|---|---|---|---|
| 统一 patch | `len(patches) == 1` | 所有候选应用同一个 `MemoryPatch` | `patches` 非空 |
| 逐条 patch | `len(patches) > 1` | 每个候选应用对应位置的 `MemoryPatch` | `selector.unit_ids` 非空且 `len(unit_ids) == len(patches)` |

逐条模式要求 `unit_ids` 非空且数量一致——因为逐条模式需要确定性的候选顺序，
只有按 `unit_ids` 指定才能保证 patch 与候选的一一对应。按 tags/scope 扫描的候选
顺序不确定，不适用于逐条模式。

统一模式适用于批量改标签、批量降权、批量修 metadata 等场景；逐条模式适用于
逐条修改 content 等需要不同 patch 的场景。

### 3. API 签名

```python
def batch_update(
    self,
    selector: UpdateSelector,
    patches: list[MemoryPatch],
    *,
    security: RequestSecurityContext,
    limit: int | None = None,
    continue_on_error: bool = True,
) -> BatchUpdateResult: ...
```

- `selector`：目标选择，各条件取「与」，至少给出一项（`unit_ids` / `scope` / `tags` /
  `before` / `after` / `filters`）。空 selector 抛 `ValidationError`。
- `patches`：修正内容数组。`len==1` 统一 patch，`len>1` 逐条对应。空列表抛
  `ValidationError`。
- `limit`：覆盖 `selector.limit`，API 层用 `replace(selector, limit=limit)` 合并。
  `None` 时沿用 selector 内的 limit。
- `continue_on_error`：`True`（默认）时单条失败不中断整批；`False` 时失败后后续标记
  `Skipped`。

### 4. 鉴权与审计

与 `batch_add`（F03 D3）一致，批量更新不能做整批粗粒度鉴权。鉴权流程：

```text
校验 selector 非空 + patches 非空 + metadata 校验 + limit 正整数校验
→ trusted_context 检查（Action.UPDATE, "batch_update"）
→ engine.permission_contexts_for_update(selector) 取候选 unit 权限上下文
→ 有候选：逐条 UPDATE 鉴权
   无候选：对 selector scope 做 UPDATE 鉴权（selector 级 fallback）
→ API 层逐条编排 _do_single_update（复用单条 update 的 schema prepare→鉴权→commit）
→ 记录 audit（candidate_count + outcome_count）
```

`permission_contexts_for_update` 是引擎方法，与 `permission_contexts_for_delete`
对称：遍历所有 scope（selector.scope 或全部 scope），按
`control/engine_impl/temporal_support.py` 的公有 `matches_update_selector` 过滤
候选 unit，返回每个候选的 `PermissionContext`。

`ENTRY_RULES` 新增 `"batch_update": EntryRule(SpaceAxis.CONTENT, SpaceAction.UPDATE)`，
与 `"update"` 对齐。

### 5. 执行链路：API 层逐条编排（引擎整批方法已删除）

初版在引擎层提供 `engine.batch_update`（保序循环调用 `update`）+
`MemoryCommandService.batch_update` 转发，约 280 行。评审发现该链路无生产调用方——
API 层为正确走「schema prepare→逐项鉴权→commit」已直接逐条编排（统一模式对候选循环
`_do_single_update`，逐条模式按 `unit_ids` 下标对应 patch，缺失 id 生成 `NotFoundError`
error outcome），引擎整批方法成为死代码，且其内部候选过滤与
`permission_contexts_for_update` 各持一份复制（三引擎各一份、时间归一行为不一致）。

最终决策：**删除引擎 `batch_update` 抽象方法与三引擎实现、删除
`MemoryCommandService.batch_update` 转发**，批量编排唯一真源在 API 层
（`api/memory_api_impl/query_ops.py`）；引擎保留 `permission_contexts_for_update`
（候选收集通道）与单条 `update`。候选判定统一到
`control/engine_impl/temporal_support.py` 的公有 `matches_update_selector`
（与 `matches_delete_selector` 同构：`unit_ids` / `tags` / `before` / `after` /
`filters` 各条件取「与」，时间比较双侧 `as_utc` 归一，`t_message` 为 `None` 时不命中；
batch_update 只命中 `ACTIVE` 单元）。

### 6. 错误隔离与 continue_on_error

`continue_on_error=True`（默认）：单条失败写入 `outcome.error` / `error_type`，
不中断后续。领域异常（`AgentMemoryError` 子类）记录原始错误信息和类型；
非领域异常记录 `"unexpected batch update failure"` / `"InternalError"` 并 log exception。

`continue_on_error=False`：失败后剩余候选标记 `error_type="Skipped"`、
`error="skipped after previous item failed"`，然后 break。所有候选（含 skipped）
都有 outcome，保持结果与候选一一对应。

### 7. HTTP 与 MCP 暴露

**HTTP**：`POST /v1/batch_update`，请求体：

```json
{
  "selector": {"unit_ids": ["id1", "id2"], "scope": {"org": "acme", "user": "u1"}},
  "patches": [{"tags": ["updated"]}],
  "limit": null,
  "continue_on_error": true
}
```

返回 `BatchUpdateResult` 的 JSON 表达：顶层 `outcomes`，每项含 `index` / `unit_id` /
`updated_unit`（成功）或 `error` / `error_type`（失败）。部分失败不使用 HTTP 207，
非法参数返回 HTTP 400。

**MCP**：`memory_batch_update(selector, patches, limit, continue_on_error)`，
docstring 面向模型撰写，说明双模式语义与 selector 形状。

## 拒绝的方案

### 方案 A：调用方自己循环 update

拒绝原因：

- 只解决提交，不解决结果对齐、partial success 和 selector 级批量语义。
- 无法在引擎内做 post-match 截断（`limit`），调用方需要先 list 再逐条 update，
  两次往返之间数据可能变化。
- 统一 patch 与逐条 patch 无法在接口层区分。

### 方案 B：复用 DeleteSelector + 统一 patch

拒绝原因：

- `DeleteSelector` 携带 `mode`（FORGET/ARCHIVE/DOWNWEIGHT/PURGE），与更新语义无关。
  更新的版本语义（SUPERSEDE/OVERWRITE）由 `MemoryPatch.mode` 逐条决定。
- `DeleteSelector` 没有 `after` 字段，无法表达「只更新某时间段之后的消息」。
- 复用会导致 selector 语义混淆——同一个 selector 既要表达选择条件又要表达操作模式。

### 方案 C：逐条模式不要求 unit_ids

拒绝原因：

- 按 tags/scope 扫描的候选顺序不确定（取决于存储遍历顺序），无法保证 patch 与候选
  的一一对应。逐条模式要求 `unit_ids` 非空且数量一致，确保确定性。

### 方案 D：limit 做 pre-match 截断（只匹配 N 条）

拒绝原因：

- pre-match 截断的语义是「只在前 N 条中匹配」，调用方无法知道 N 条之后的候选
  是否也满足条件。post-match 截断的语义是「全量匹配后取前 N 条执行」，调用方
  知道匹配范围，只控制执行量。post-match 更符合「最多更新 N 条」的直觉。

## 验证

第一版实现覆盖（全部通过）：

1. 统一 patch + `unit_ids`：3 条候选全部成功，tags 更新生效。
2. 统一 patch + scope+tags 扫描：只命中带指定 tag 的 unit，不影响其他 unit。
3. 逐条 patch：2 条候选分别应用不同 patch，content 各异。
4. `continue_on_error=True`：FORGOTTEN unit 的 update 失败但其他成功。
5. `continue_on_error=False`：失败后后续标记 Skipped。
6. 空 selector 抛 `ValidationError`。
7. 空 patches 抛 `ValidationError`。
8. 逐条模式 unit_ids 与 patches 数量不匹配抛 `ValidationError`。
9. 逐条模式无 unit_ids 抛 `ValidationError`。
10. 内核保留 system_metadata key 被拒绝。
11. `selector.limit=0/-1` 抛 `ValidationError`（显式契约拒绝，不静默忽略）。
12. 逐条模式 `limit < len(patches)` 抛 `ValidationError`（下界承诺，不截断候选集）。
13. 统一 patch 更新 tier 成功。
14. HTTP `/v1/batch_update` 统一 patch 端到端验证（3 条成功，旧版 superseded）。
15. HTTP 逐条 patch 端到端验证。
16. HTTP `continue_on_error` 端到端验证。
17. HTTP 数量不匹配返回 400。
18. HTTP 空 selector 返回 400。

## 已知遗留

1. **并发调度暂不做**：第一版默认串行保序。后续可按 scope 分组并发，但同 scope 内
   必须保序。
2. **候选顺序不确定性**：统一模式下候选顺序取决于存储遍历顺序，未做排序保证。
   `limit` 截断在前 N 条上发生，调用方如需确定性应使用 `unit_ids`。
3. **跨 item 事务暂不做**：批量更新不是 all-or-nothing 事务。需要事务语义的修正
   应在更高层维护 staging 和补偿。
4. ~~**AgentEngine 与 CloudEngine/InMemoryEngine 的时间比较差异**~~（已解决：候选
   判定统一到 `temporal_support.matches_update_selector`，双侧 `as_utc` 归一，三引擎
   共用唯一实现）。
