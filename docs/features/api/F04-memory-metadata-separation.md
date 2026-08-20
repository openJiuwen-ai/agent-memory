# F04 — Memory Metadata 双命名空间分离

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-18 |
| 影响范围 | `jiuwen_memory/common/type_def/`、`jiuwen_memory/api/`、`jiuwen_memory/control/`、`jiuwen_memory/ingest/`、`jiuwen_memory/construction/`、`jiuwen_memory/retrieval/`、`bootstrap/` |
| 测试基线 | `pytest -m unit`：全部通过（外部依赖/真实 LLM 用例按原有标记跳过） |
| Refs | — |

> 状态：已实现。本文同时记录稳定决策与落地边界。

## 1. 背景

本次提交将 `MemoryUnit.metadata` 拆分为 `system_metadata` 和 `user_metadata`，并同步调整写入、更新、
索引和过滤链路，使系统只解释系统字段，用户字段只承载业务数据及其存储、返回和检索语义。

修改前，`MemoryUnit.metadata` 同时承载：

- 系统控制和状态，例如 `infer`、`procedural`、`middle`、`pipeline`、`importance`；
- 用户自定义字段，例如 `project`、`priority`、`department`。

Engine、Pipeline、Evolver 等组件会读取其中部分 key。用户如果写入同名字段，就可能意外改变系统
行为；索引和过滤也无法说明一个字段属于用户还是系统。

本次重构只解决以下问题：

1. 用户字段与系统字段在模型、API、真源和索引中明确分离；
2. 系统逻辑只解释系统字段；
3. 用户字段可以持久化、索引和过滤；
4. 写入、批量写入、更新和派生链路使用同一规则。

本设计不考虑旧混合 `metadata` 数据的兼容迁移。若需要迁移，应单独设计离线映射规则，不在运行时
猜测旧 key 的归属。

## 2. 设计原则

### 2.1 只增加两个明确字段

`MemoryUnit` 使用：

```python
MetadataValueType = str | int | float | bool | None | list[str]


@dataclass
class MemoryUnit:
    ...
    system_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    user_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
```

不再保留语义不明确的 `MemoryUnit.metadata`，也不增加第三个 metadata 容器。

`system_metadata` 和 `user_metadata` 不封装成 class，也不分别定义类型别名。二者直接使用同一个
`MetadataValueType`：

- 字段名已经能够表达归属；
- 普通 dict 与现有 JSON、codec、Store 接口最接近；
- 不需要额外的构造、解包和序列化规则。

### 2.2 字段分为三类

| 类型 | 示例 | 放置位置 |
|---|---|---|
| 核心结构化字段 | `scope`、`tier`、`tags`、`lifecycle`、`temporal`、`provenance` | `MemoryUnit` 一等字段 |
| 系统扩展字段 | `infer`、`procedural`、`middle`、`pipeline`、`importance`、prompt key | `system_metadata` |
| 用户扩展字段 | `project`、`priority`、`department`、用户自定义的 `infer` | `user_metadata` |

本次只移动字段归属，不顺带重命名现有系统 key。例如当前 `infer` 进入
`system_metadata["infer"]`，不改成新的多级命名。系统 key 的统一命名可以后续单独评估，不能扩大本次
修改范围。

### 2.3 用户字段对系统不透明

系统可以对 `user_metadata` 做：

- 类型校验；
- 持久化和编解码；
- 索引和严格类型过滤；
- API 返回；
- 按固定规则传播到派生记忆。

系统不得解释用户字段的业务语义，也不得根据用户字段改变控制流。例如：

```python
system_metadata = {"infer": True}
user_metadata = {"infer": False}
```

Engine 只读取：

```python
infer = as_bool(system_metadata.get("infer", False))
```

禁止 fallback 到 `user_metadata`：

```python
# 禁止
infer = as_bool(
    system_metadata.get("infer", user_metadata.get("infer"))
)
```

同理，`user_metadata["priority"]` 可以参与过滤，但不能自动等价于系统的 `importance`。如果业务确实
需要转换，应由调用方或明确的业务适配代码生成相应 `system_metadata`，Engine 不猜测这种关系。

## 3. API 修改

### 3.1 修改总览

| 接口或类型 | 当前契约 | 目标契约 | 修改位置 |
|---|---|---|---|
| `MemoryAPI.add/add_async` | `metadata` | `system_metadata` 和 `user_metadata` | `jiuwen_memory/api/memory_api.py`、`memory_api_impl/local_memory_api.py` |
| `MemoryEngine.write` | `metadata` | `system_metadata` 和 `user_metadata`；控制分支只读取 `system_metadata` | `jiuwen_memory/control/engine.py`、`engine_impl/` |
| `RawPayload` | `metadata` | `system_metadata` 和 `user_metadata` | `jiuwen_memory/common/type_def/raw.py` |
| `MemoryUnit` | `metadata` | `system_metadata` 和 `user_metadata` | `jiuwen_memory/common/type_def/memory.py`、codec |
| `BatchWriteItem` | `metadata` | `system_metadata` 和 `user_metadata` | `jiuwen_memory/control/types.py`、batch 归一化逻辑 |
| `MemoryPatch` | `metadata` | `system_metadata` 和 `user_metadata`，均保持 dict 合并语义 | `jiuwen_memory/control/types.py`、Engine update |
| `search/list.filters` | `metadata.<key>` | 用户输入改用 `user_metadata.<key>` | API、Retrieval、Store filter compiler |
| `RetrievedItem` | 不返回 metadata | 增加 `user_metadata` | `jiuwen_memory/retrieval/types.py`、Discloser |
| HTTP/SDK add、batch、update | `metadata` | 按接入形态暴露 `system_metadata` 和 `user_metadata` | `bootstrap/core/handler.py`、SDK/plugin adapter |

`get`、`delete`、`evolve` 等方法不需要增加 metadata 参数。它们继续使用 `MemoryUnit` 或已有 selector，
只适配新的字段名。

### 3.2 单条写入

```python
def add(
    self,
    content: str,
    scope: Scope,
    source: Modality = Modality.TEXT,
    *,
    identity: Scope,
    assets: list[str] | None = None,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
    occurred_at: datetime | None = None,
) -> list[MemoryUnit]:
    ...


async def add_async(...同参数...) -> list[MemoryUnit]:
    ...
```

示例：

```python
api.add(
    "用户喜欢喝咖啡",
    scope,
    identity=identity,
    system_metadata={"infer": True, "memory_type": "semantic"},
    user_metadata={"project": "assistant", "priority": 8, "infer": False},
)
```

`add` 和 `add_async` 不再接受旧 `metadata`。不根据 key 自动拆分，也不允许一个命名空间覆盖另一个
命名空间。

### 3.3 API 到 MemoryUnit 的完整链路

```text
MemoryAPI.add/add_async
    │  校验、鉴权、审计、参数装配
    ▼
MemoryEngine.write
    │  解释 system_metadata；透传 user_metadata
    ▼
RawPayload(system_metadata, user_metadata)
    ▼
Ingestor.ingest
    │  构造 MemoryUnit
    ▼
MemoryUnit(system_metadata, user_metadata)
```

API 只校验和委托：

```python
async def add_async(
    ...,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
) -> list[MemoryUnit]:
    validate_metadata(system_metadata)
    validate_metadata(user_metadata)

    return await self._engine.write(
        content,
        scope,
        source,
        assets=assets,
        tags=tags,
        system_metadata=system_metadata,
        user_metadata=user_metadata,
        occurred_at=occurred_at,
    )
```

Engine 显式接收并分别复制：

```python
async def write(
    self,
    content: str,
    scope: Scope,
    source: Modality = Modality.TEXT,
    *,
    assets: list[str] | None = None,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
    occurred_at: datetime | None = None,
) -> list[MemoryUnit]:
    system = dict(system_metadata or {})
    user = dict(user_metadata or {})

    infer = as_bool(system.get("infer", False))
    procedural = as_bool(system.get("procedural", False))
    middle = as_bool(system.get("middle", False))

    payload = RawPayload(
        id=str(uuid.uuid4()),
        scope=scope,
        modality=source,
        data=content.encode("utf-8"),
        system_metadata=system,
        user_metadata=user,
        occurred_at=occurred_at,
    )
    units = self._ingestor.ingest([payload])
```

`RawPayload` 和 Ingestor 负责把数据写入初始 `MemoryUnit`：

```python
@dataclass
class RawPayload:
    ...
    system_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    user_metadata: dict[str, MetadataValueType] = field(default_factory=dict)


MemoryUnit(
    ...,
    system_metadata=dict(payload.system_metadata),
    user_metadata=dict(payload.user_metadata),
)
```

API 不判断 `infer`、不构造 `MemoryUnit`、不操作 Storage 或 IndexBuilder，因此仍是薄封装。
Engine 负责控制流，Ingestor 负责创建初始领域对象。

### 3.4 批量写入

```python
@dataclass
class BatchWriteItem:
    content: str
    scope: Scope | None = None
    source: Modality | None = None
    assets: list[str] | None = None
    tags: list[str] | None = None
    system_metadata: dict[str, MetadataValueType] | None = None
    user_metadata: dict[str, MetadataValueType] | None = None
    occurred_at: datetime | None = None
    stream_id: str = ""
    sequence: int | None = None
    idempotency_key: str = ""
```

顶层默认值与 item 值在各自命名空间内合并，item 优先：

```python
system_metadata = {
    **(default_system_metadata or {}),
    **(item.system_metadata or {}),
}
user_metadata = {
    **(default_user_metadata or {}),
    **(item.user_metadata or {}),
}
```

其余顺序、错误和 partial-success 语义保持不变。

### 3.5 更新

沿用现有 `MemoryPatch`，只把一个 `metadata` 字段替换成两个 dict 字段：

```python
@dataclass
class MemoryPatch:
    content: str | None = None
    tier: MemoryTier | None = None
    tags: list[str] | None = None
    system_metadata: dict[str, MetadataValueType] | None = None
    user_metadata: dict[str, MetadataValueType] | None = None
    t_valid: datetime | None = None
    t_invalid: datetime | None = None
    mode: UpdateMode = UpdateMode.SUPERSEDE
```

两个字段都保持当前的合并更新语义：

```python
if patch.system_metadata is not None:
    new.system_metadata.update(patch.system_metadata)
if patch.user_metadata is not None:
    new.user_metadata.update(patch.user_metadata)
```

例如更新系统状态和用户字段：

```python
MemoryPatch(
    system_metadata={"dreaming": True},
    user_metadata={"priority": 9},
)
```

本次不增加 metadata key 删除语义。若以后确有删除需求，再独立扩展 update 契约；不能为尚未提出的
需求预先增加 patch 类型。

### 3.6 查询与过滤

`search` 和 `list` 的方法参数不变，仍使用 `FilterExpr`。用户字段路径改为：

```python
filters={
    "AND": [
        {"user_metadata.project": "assistant"},
        {"user_metadata.priority": {"gte": 8}},
        {"tags": {"contains": "preference"}},
    ]
}
```

规则：

- 公开用户过滤使用 `user_metadata.<key>`；
- `tags`、`tier`、`lifecycle` 和时间等一级字段保持原名；
- 内部组件如需按系统字段过滤，使用 `system_metadata.<key>`；
- API 必须先校验用户表达式，再追加内部系统谓词；
- `metadata.<key>` 不再作为新契约的别名。
- 为降低调用方迁移成本，裸自定义字段（如 `project`）仅在 FilterExpr
  规范化边界映射为 `user_metadata.project`；内核与 Store 只看到完整规范路径。

### 3.7 返回结构

Core API 的 `add`、`get`、`update`、`list`、`inspect` 和 `trace` 返回新的 `MemoryUnit`，因此自然包含
`system_metadata` 和 `user_metadata`。

`RetrievedItem` 当前不是完整 `MemoryUnit`。为了让搜索调用方取得自定义字段，增加
`user_metadata`：

```python
@dataclass
class RetrievedItem:
    ...
    user_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
```

普通搜索结果不增加 `system_metadata`；系统诊断信息继续通过 `get`、`inspect` 或 trajectory 获取。

## 4. ToB 与 ToC 接入

内核始终使用同一个 `MemoryUnit`，不为 ToB、ToC 定义不同的数据模型。

- ToC 接口只暴露 `user_metadata`，服务端按配置补充 `system_metadata`；
- ToB 或可信内部接口可以同时暴露 `system_metadata` 和 `user_metadata`；
- 接入层不允许写系统字段时，应拒绝该字段，不能转存到 `user_metadata`；
- 返回时是否展示 `system_metadata` 由接入形态决定，不改变 Core API 和真源模型。

ToC 请求示例：

```json
{
  "content": "用户喜欢喝咖啡",
  "user_metadata": {
    "project": "assistant",
    "priority": 8
  }
}
```

ToB 请求示例：

```json
{
  "content": "用户喜欢喝咖啡",
  "system_metadata": {
    "infer": true,
    "memory_type": "semantic"
  },
  "user_metadata": {
    "project": "assistant",
    "priority": 8
  }
}
```

本设计只规定暴露边界，不增加新的访问策略类型；具体鉴权沿用安全模块现有机制。

## 5. 持久化、索引与过滤

### 5.1 真源

codec 分别序列化：

```json
{
  "system_metadata": {},
  "user_metadata": {}
}
```

由于旧 `metadata` 无法可靠判断字段归属，本次按破坏性模型变更处理。新 codec 不在运行时自动拆分
旧数据。

### 5.2 索引投影

为保持当前 metadata 过滤能力并减少特殊规则，IndexBuilder 将两个命名空间都写入索引，但保留路径
边界：

```text
system_metadata.memory_type
system_metadata.pipeline
user_metadata.project
user_metadata.priority
```

一级系统字段继续独立投影：

```text
unit_id
tier
lifecycle
tags
source
t_event
t_valid
t_invalid
t_message
```

本次不引入系统字段 registry、`indexed=True` 声明或新的索引策略接口。公开 API 是否允许过滤系统
字段由 API 边界决定，不依赖索引层隐藏字段。

### 5.3 过滤一致性

- Store 在 top-k 前下推完整 FilterExpr；
- UnitReader 从 `MemoryUnit.user_metadata` 复核 `user_metadata.<key>`；
- UnitReader 从 `MemoryUnit.system_metadata` 复核内部 `system_metadata.<key>`；
- 不允许从一个命名空间 fallback 到另一个命名空间；
- IndexBuilder 和 UnitReader 必须对同一逻辑路径读取同一值。

## 6. 派生记忆传播

派生链路必须明确处理 `user_metadata`，避免不同 Extractor 行为不一致。

### 6.1 `user_metadata`

- 单一来源派生：复制来源 `user_metadata`；
- 多来源派生：只保留所有来源都存在且值相等的字段；
- 冲突字段不传播。

本次不增加可插拔合并策略。若以后出现明确的业务合并需求，再单独设计。

### 6.2 `system_metadata`

系统字段不整体复制。派生组件只写入自己产生的系统字段，并按现有业务需要显式保留路由等必要
字段。写入控制字段 `infer`、`procedural`、`middle` 不传播到派生记忆，避免再次触发控制行为。

## 7. 校验与错误

`system_metadata` 和 `user_metadata` 共用同一个基础 value 校验：

- key 必须是非空字符串；
- value 只允许 `MetadataValueType`；
- 浮点数必须有限；
- 不接受嵌套对象和任意类型数组；
- 字符串、数字和布尔值不做隐式转换。

额外规则：

- Python Core API 传入旧 `metadata` 因签名已移除而失败；HTTP add/batch/update
  边界显式返回 `ValidationError`；
- 公开用户 filters 引用 `system_metadata.<key>`：按接入形态拒绝；
- codec 读取未迁移的旧混合数据：明确报迁移错误；
- 错误信息指出字段路径和原因，不回显敏感值。

## 8. 改造范围

| 范围 | 必须修改的内容 |
|---|---|
| common | `MemoryUnit`、`RawPayload`、codec、memory filter、公共类型导出 |
| API | add/add_async、batch、update、返回视图、metadata 校验 |
| control | Engine.write、MemoryPatch、系统字段读取、更新逻辑 |
| ingest | RawPayload 到 MemoryUnit 的字段复制 |
| construction | Extractor/Evolver 传播、IndexBuilder 投影 |
| retrieval | FilterExpr 字段解析、Store 下推、UnitReader 复核、RetrievedItem |
| bootstrap/SDK/plugin | 请求和响应 schema、旧 `metadata` 参数移除 |
| docs | S02/S03/S04/S06/S07、受影响模块 AGENTS.md、对应 feature 归档 |

不要机械地把所有 `.metadata` 替换成 `.user_metadata`：

- 当前被 Engine、Pipeline、Evolver、Lifecycle 等解释的字段进入 `system_metadata`；
- 当前调用方业务字段进入 `user_metadata`；
- `PermissionContext.metadata`、Space metadata、Store record metadata 等其他模型按自身语义处理，不在
  本次修改中机械改名。

当前 `TRANSIENT_SYSTEM_METADATA_KEYS`（S09 起的名字，作用域为 `system_metadata`）允许把任意运行时对象临时塞进 metadata，这与统一
`MetadataValueType` 不兼容，也不属于持久元数据语义。落地前应先核对实际调用：未使用的瞬态 key
直接删除；仍在使用的值改走已有 extensions、依赖注入或明确参数，不能通过放宽
`MetadataValueType` 保留对象透传。CloudEngine 当前对 metadata 的字符串归一化也应移除，两个
命名空间都保留 JSON 原生类型。

## 9. 验证

至少覆盖：

1. `system_metadata["infer"]=true`、`user_metadata["infer"]=false` 时走 infer 路径；
2. 只有 `user_metadata["infer"]=true` 时不改变系统行为；
3. API 将 `user_metadata` 完整传到 Engine、RawPayload 和 MemoryUnit；
4. batch 默认值与 item 值在各自命名空间内合并，item 优先；
5. `MemoryPatch` 分别合并更新 `system_metadata` 和 `user_metadata`；
6. 用户字段可以按字符串、数值、布尔和 `list[str]` 正确过滤；
7. IndexBuilder 前置过滤与 UnitReader 真源复核结果一致；
8. 同名系统字段和用户字段可以共存且互不影响；
9. 单来源派生复制用户字段，多来源冲突字段不传播；
10. 派生记忆不继承 `infer`、`procedural`、`middle`；
11. Core API 返回两个字段，普通 `RetrievedItem` 返回 `user_metadata`；
12. 旧 `metadata` 入参和未迁移 codec 数据明确失败，不静默猜测。

已实现上述链路并运行全量 unit marker 测试。仓库原有的真实 LLM 和未安装
Elasticsearch/PostgreSQL 依赖用例继续按原有 skip 条件处理。

## 10. 拒绝的方案

### 10.1 保留一个 `metadata`，依靠保留 key

保留列表只能覆盖已知系统字段，新增字段仍可能与用户字段冲突，不能满足结构性隔离需求。

### 10.2 只修改 API，MemoryUnit 内部继续混合

冲突会从 API 推迟到 Ingestor、真源和索引，系统仍然无法证明字段归属。

### 10.3 为 metadata 增加包装 class

字段名已经表达系统和用户归属。包装 class 不增加必要语义，却增加构造、序列化、过滤和 patch
转换成本。

### 10.4 为本次修改增加 registry、访问策略或合并插件

系统 key 注册、ToB/ToC 访问策略对象和用户字段自定义合并都可以在出现明确需求后独立设计，不是
完成本次双命名空间需求的前置条件。

## 11. 已知遗留

- 旧混合 `metadata` 的离线迁移不在本特性范围内；
- 系统 key 的统一命名不在本特性范围内，当前 key 保持不变；
- `db_query_service` / `encryption_port` 等运行时对象已不经 MemoryUnit metadata
  透传，仅保留在查询 `extensions` 的局部兼容路径。

## 12. 文档同步

本特性落地后已同步：

- 更新 `docs/specs/S02-memory-api.md`：公共 API、MemoryPatch、过滤和返回契约；
- 更新 `docs/specs/S03-control.md`：Engine 写入和派生传播；
- 更新 `docs/specs/S04-retrieval.md`：用户过滤与内部系统谓词；
- 更新 `docs/specs/S06-storage.md`：索引路径和后端过滤；
- 更新 `docs/specs/S07-common.md`：MemoryUnit、RawPayload 和 codec；
- 更新受影响模块的 `AGENTS.md`。
