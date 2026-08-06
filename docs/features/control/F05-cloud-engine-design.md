# F05 — CloudEngine 读写编排设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27 |
| 影响范围 | `src/control/engine_impl/cloud_engine.py`、`src/control/engine_impl/__init__.py`、`src/control/pipeline.py`、`src/construction/`、`src/common/encryption/`、`src/storage/kv_impl/`、`docs/specs/S03-control.md`、`docs/specs/S06-storage.md`、`docs/specs/S07-common.md` |
| 测试基线 | 已新增 `tests/unit/control/test_cloud_engine.py`；本地无 pytest，使用 `runpy` 显式调用测试函数通过 |

## 背景

云侧部署需要在同一个内核内同时满足几类能力：

- 不同 `message_type` 使用不同抽取提示词、模型配置、分类器、索引构建器和检索器。
- 多租户隔离以 `org + space` 为硬边界，所有写入、检索、治理和演进都不能隐式跨 space。
- 真源 KV 写入前加密，读取后解密，并满足安全设计中的 `ENC1` 信封、AAD 绑定和 fail-closed 要求。
- 对外接口仍保持 `MemoryAPI -> MemoryEngine` 的薄委托关系，鉴权和审计继续在 API 层完成。

现有 `InMemoryEngine` 已经支持基础 write/get/recall/update/delete/evolve 流程，也能通过
`MemoryPipeline` 按 metadata 路由构建和查询 profile。但它的定位是本地最小实现：

- 默认抽取和模型配置依赖单组构建算子。
- 显式 `evolve(scope, mode)` 仍走单一 Scheduler/Evolver。
- 没有云侧安全配置、space 强约束和加密 KV 的装配约定。

因此需要新增 `cloud_engine.py`，作为面向云侧部署的读写编排实现。它不替代
`MemoryEngine` 抽象接口，也不把加密、抽取算法、权限判断实现塞进 engine，而是在 control
层把已装配好的安全、存储、构建和检索能力编排起来。

## 决策

### 决策 1：新增独立 `CloudEngine` 实现，不继承 `InMemoryEngine`

`CloudEngine` 放在 `src/control/engine_impl/cloud_engine.py`，直接实现 `MemoryEngine`，并通过
`EngineProducer` 注册为 `cloud`。`src/control/engine_impl/__init__.py` import 该模块触发自注册。

`CloudEngine` 不继承 `InMemoryEngine`，也不访问其 `_kv`、`_pipeline`、`_index` 等受保护成员。
原因是云侧编排会涉及 message type、space、安全 KV、profile-aware evolve 等新约束，继承旧实现容易形成隐式耦合，后续修改也容易绕过安全接缝。

`CloudEngine` 仍保持 control 层既有不变量：

1. Engine 不执行鉴权，API 层通过 `PermissionManager.check` 后只传已鉴权 target `scope`。
2. Engine 不直接调用 LLM，抽取、分类、升华、关联和冲突消解都由 construction 算子完成。
3. Engine 不绑定具体存储后端，只依赖注入的 Store 抽象。
4. Engine 不长期持有明文缓存，不把 content 写入日志或异常消息。

### 决策 2：`message_type` 是云侧路由键，区别于记忆认知类型

云侧读写编排以 `message_type` 作为输入消息类型，例如：

- `chat`
- `coding`
- `tool_result`
- `execution_trace`
- `document`

`message_type` 表示输入消息来源、格式和抽取策略，不等同于 `MemoryUnit.tier`
或 `metadata["memory_type"]`。后者描述记忆本身的认知角色或策略分类，例如 episodic、semantic、
procedural、preference。

第一阶段保持 `MemoryEngine.write` 抽象接口兼容：调用侧可通过
`metadata["message_type"]` 下传。HTTP/SDK 等 surface 后续可以增加显式 `message_type`
字段，但进入 API 层后必须归一化到 metadata，保证 engine 只消费统一内部结构。

写入后，`CloudEngine` 必须把路由信息固化到真源：

- `metadata["message_type"]`
- `metadata["pipeline"]`
- 选中 profile 需要的其他治理字段

这样 `get/update/delete` 的权限上下文可以从真源解析，不能信任调用方重新声明的
`message_type`。

### 决策 3：不同提示词和模型通过 Pipeline profile 装配，不在 Engine 硬编码

`CloudEngine` 复用 `MemoryPipeline` 做 profile 选择。云侧默认 route key 建议配置为
`message_type`：

```yaml
pipeline:
  default:
    target: metadata
    params:
      route_key: message_type
      fallback: chat
      routes:
        coding: coding
        tool_result: tool_result
        execution_trace: execution_trace
      profiles:
        chat:
          index_builder: chat_index
          retriever: chat_retriever
          evolver: chat_evolver
          classifier: chat_classifier
        coding:
          index_builder: coding_index
          retriever: coding_retriever
          evolver: coding_evolver
          classifier: coding_classifier
```

不同提示词和模型配置落在 construction/common 的具名组件上：

```yaml
llm:
  coding_llm:
    target: openai
    params:
      model: gpt-5-codex
  chat_llm:
    target: openai
    params:
      model: gpt-5-mini

extractor:
  coding_extractor:
    target: llm
    params:
      llm: coding_llm
      prompt_profile: coding
  chat_extractor:
    target: llm
    params:
      llm: chat_llm
      prompt_profile: chat
```

`CloudEngine` 只负责选择 profile 并调用选中 profile 的 `Evolver`、`Classifier`、`IndexBuilder`
和 `Retriever`。它不能根据 `message_type` 拼 prompt，也不能根据 `message_type`
直接选择某个模型。

### 决策 4：写入路径按 profile 编排三类写入模式

`CloudEngine.write` 以选中 profile 为中心编排，保留现有写入语义，并加入云侧元数据固化：

```text
RawPayload
  -> Ingestor.ingest
  -> 补齐 Scope / metadata / tags / occurred_at
  -> MemoryPipeline.select_for_write
  -> 固化 metadata["message_type"] 与 metadata["pipeline"]
  -> 根据写入模式分流
```

写入模式分三类：

| 模式 | 触发条件 | 编排行为 |
|---|---|---|
| `raw_indexed` | 默认，`infer` 与 `procedural` 均不为 true | 选中 profile 的 classifier 分类，落 `/memory/{id}`，构建索引，返回原始 memory unit |
| `infer_extract` | `metadata["infer"] == "true"` | 原文落 `/messages/{id}` 作为上下文，不建索引；选中 profile 的 evolver 执行 `EXTRACT`，返回派生 memory unit |
| `procedural_extract` | `metadata["procedural"] == "true"` | 原文不作为普通记忆落 `/memory/`；选中 profile 的 evolver 汇总为过程记忆并落真源与索引 |

所有写入都必须满足：

- 写入 target scope 必须与产出的 `MemoryUnit.scope` 一致。
- 写入 target scope 已由 API 鉴权，engine 不重复 check。
- 真源落盘只通过注入的 `KVStore`。
- 索引构建只通过选中 profile 的 `IndexBuilder`。
- 返回值来自真源或刚构建的明文 `MemoryUnit`，不返回密文字节。

### 决策 5：读取路径保持明文对象语义，加密在 KV 装饰器内透明完成

`CloudEngine` 的 `get`、`recall`、`update`、`delete`、`permission_context_for_unit` 和
`permission_contexts_for_delete` 都只处理明文 `MemoryUnit` 对象。

加解密不在 engine 方法里手写，而通过 `EncryptedKVStore` 透明完成：

```text
CloudEngine
  -> KVStore.insert/update/get/list/scan
  -> EncryptedKVStore
  -> Raw KVStore
```

这样所有直接读取 KV 的路径都能统一加解密，包括：

- get 点读
- recall 结果 materialize
- update 读旧写新
- delete selector 扫描
- lifecycle 扫描
- governance inspect/trace
- evolver 读取 `/messages/` 上下文

`CloudEngine` 不允许为了性能绕过 KVStore 直接访问底层 raw store。若加密开启，底层 raw store
只能看到 `ENC1` 密文字节；若加密关闭，装配层直接使用原始 KVStore。系统没有“空实现
encryptor”，避免配置声称启用加密但实际透传明文。

### 决策 6：安全接口放 `common/encryption`，KV 装饰器放 `storage/kv_impl`

云侧安全能力拆成两层：

| 层 | 位置 | 职责 |
|---|---|---|
| 安全接口与加密实现 | `src/common/encryption/` | `EncryptionProvider`、`EncryptionContext`、本地密钥实现、`ENC1` envelope、加密错误 |
| KV 加密装饰器 | `src/storage/kv_impl/encrypted_kv_store.py` | 实现 `KVStore`，写前加密、读后解密、明文兼容、fail-closed |

`EncryptionContext` 与显式 AAD 至少绑定：

- `org`
- `space`
- KV key
- value 用途：`memory_unit`、`raw_message` 或通用 `kv_value`
- AAD 格式版本

`org + space + key` 绑定可以防止密文被复制到另一个 space 或另一个 key 后仍被成功解密。
不是 `ENC1` 的老数据可按明文兼容读取；只要是 `ENC1` 信封，解密失败必须 fail-closed。

### 决策 7：Space 隔离由 Scope 和 Store 强制，Engine 只做一致性校验

`CloudEngine` 不从 request payload 解析租户身份，也不自行推导 actor。API/bootstrap 层负责把
外部 `org/space/user/agent/session` 归一化为 target `Scope` 和 actor `Scope`。

`CloudEngine` 只做两类一致性校验：

1. 本次写入产生的每个 `MemoryUnit.scope` 必须等于 target scope。
2. 从真源读出的已有 unit，其 `unit.scope` 必须与调用传入的 target scope 兼容；否则视为越界数据或坏数据，拒绝继续更新/删除。

真正的隔离强制点在：

- API 层：`require_space=true` 时拒绝缺少 space 的数据面请求。
- PermissionManager：先校验 `org + space`，再按 space policy 的 `principal_path` 判断 owner-cover。
- Store：所有 KV/vector/fulltext/graph/fusion/fs 操作都以 `org + space` 纳入 namespace、partition 或强制 filter。
- Security AAD：加密上下文绑定 `org + space + key`。

默认 recall 永远只在一个 target space 内执行。跨 space recall 必须由 API 显式传入已授权 scopes
或 shared space 配置完成，底层 retriever 不得自行扩大范围。

### 决策 8：显式 evolve 需要 profile-aware 执行模型

`write(infer=true)` 可以直接使用本次消息的 `message_type` 选择 profile；但
`evolve(scope, mode)` 的现有签名只有 `scope/mode/channel`，没有 `message_type`。
云侧不能继续用单一 Evolver 扫描整个 scope，否则不同 message type 的后台演进会走错提示词和模型。

目标模型是：一次 scope 级 evolve job 内部按真源 metadata 分组执行。

```text
CloudEngine.evolve(scope, mode)
  -> 提交一个 scope 级 job
  -> executor 扫描 scope 内 /memory/ 记录
  -> 按 metadata["message_type"] 或 metadata["pipeline"] 分组
  -> 每组调用对应 PipelineBinding.evolver
  -> 合并 EvolveResult 并记录 job detail
```

第一阶段可以保留 `Scheduler.submit(scope, mode, channel)` 的公共接口，但 cloud 装配需要一个
profile-aware executor 或 cloud scheduler。若暂时复用现有 `InProcessScheduler`，显式
`evolve` 只能作为已知降级能力，不能宣称完全支持 message_type 路由。

### 决策 9：权限上下文必须从真源元数据解析

CloudEngine 需要继续提供：

- `permission_context_for_unit`
- `permission_contexts_for_delete`

这些方法只返回鉴权所需元数据，不返回 content/assets。上下文字段包括：

- `resource_type`
- `message_type`
- `memory_type`
- `pipeline`
- `unit_id`
- `scope`
- `tags`
- `metadata`

对于 `get/update/delete`，API 层应先做基础 scope 门槛，再调用 CloudEngine 从真源解析上下文，然后按上下文做二次权限检查。调用方传入的 `message_type` 或 `memory_type` 不能用于已有 unit 操作的最终权限路由。

### 决策 10：CloudEngine 不承担 Space 管理 API，但提供数据面清理原语

Space 的创建、归档、删除、导出、成员管理、policy 管理和用量统计属于管理面能力，仍由
`MemoryAPI` + `SpaceManager` 承接。`CloudEngine` 只额外提供 `purge_space(org, space)` 内部
原语，用于删除目标 Space 下全部子 Scope 的记忆真源和索引；它不改变 Space metadata、
policy、member 或状态。

当前 `CloudEngine` 不直接读取 `SpaceManager` policy。已接入执行路径的只有：

- `principal_path`：由 API 读取 SpacePolicy 后注入权限上下文。
- `scope.require_space`：来自全局 PolicyManager，并非 SpacePolicy.require_space。

SpacePolicy 中的 `require_space`、`pipeline_profiles`、`index_profiles`、
`storage_isolation_strategy`、retention 和 quotas 当前仅持久化，尚未接入 Engine/Store
执行路径。

## 拒绝的方案

拒绝直接修改 `InMemoryEngine` 承载云侧能力。这样会把本地最小实现和云侧强隔离、安全合规、profile-aware evolve 绑在一起，破坏旧路径的简单性。

拒绝让 `CloudEngine` 直接调用 `EncryptionProvider.encrypt/decrypt`。加密是所有 KV 路径的
横切能力，应该由 `EncryptedKVStore` 统一保证，否则治理、生命周期、evolver 上下文等路径
容易漏加密。

拒绝把 prompt 和 model 选择硬编码在 `CloudEngine`。提示词和模型属于 construction/common 组件配置，engine 只做 profile 编排。

拒绝把 `message_type` 直接复用为 `MemoryUnit.tier`。`message_type` 是输入消息类型，`tier` 是记忆认知角色；两者混用会让检索、权限和抽取策略难以演进。

拒绝默认跨 space 聚合检索。云侧默认数据面操作只作用于单个 target space；共享必须显式授权或显式查询 shared space。

拒绝让 `identity` 下沉到 CloudEngine。鉴权和审计的 PEP 在 API 层，engine 只接收已鉴权 target scope。

## 验证

已新增单元测试覆盖：

- `InMemoryEngine` 拒绝非空 `space`，命名 Space 的数据面行为只由 `CloudEngine` 承担。
- 不同 Scope 可使用相同 `MemoryUnit.id`，CloudEngine 删除与生命周期操作只影响目标 Scope。
- `list` 对实际返回的每个 unit 使用真源权限上下文做二次鉴权。
- `delete_space` 通过 CloudEngine 清理目标 Space 下全部子 Scope。
- `message_type=coding` 默认写入选择 coding profile 的 classifier 和 index_builder。
- `message_type=chat` 写入选择 chat profile，且 metadata 固化 `message_type` 与 `pipeline`。
- `recall` 按 `RetrievalQuery.extensions["message_type"]` 选择 retriever。
- `permission_context_for_unit` 从真源 metadata 解析 `message_type` / `pipeline` / `memory_type`。
- `infer=true` 使用所选 profile 的 evolver，并从真源回读派生明文 `MemoryUnit`。
- `update(OVERWRITE)` 修改 `message_type` 时从旧 profile 索引移除，并写入新 profile 索引。

仍需后续集成测试覆盖：

- `get/update/delete` 更完整组合路径按真源 metadata 选择旧/新 profile 的 index update/remove。
- 开启 `EncryptedKVStore` 后，底层 raw KV 不包含明文 content，CloudEngine 读取仍返回明文 `MemoryUnit`。
- `ENC1` 解密失败 fail-closed，不 fallback 到密文字节。
- 不同 `space` 下同 content 互不召回。
- `require_space=true` 时缺少 space 的数据面请求被 API 层拒绝。
- 显式 `evolve(scope, mode)` 在 cloud executor 中按 profile 分组执行，或在未支持时明确测试降级行为。

## 已知遗留

- `Scope.space`、storage scope key、API payload、PermissionManager owner-cover、CloudEngine
  按完整 Scope 的 get/update/delete/lifecycle/index 清理已落地；仍需补不同 `space` 下相同
  content 的端到端 recall 集成测试。
- `common/encryption` 接口、`local` EncryptionProvider 和 `EncryptedKVStore` wrapper 已落地；CloudEngine 仍需补开启 `EncryptedKVStore` 后的端到端静态加密集成测试。
- 现有 `Scheduler` 接口没有 job context；当前 `CloudEngine.evolve(scope, mode)` 仍委托注入的 Scheduler，profile-aware evolve 需要新增 cloud executor 或扩展 Scheduler 规约。
  - **增量（2026-07，[`F06`](F06-middle-term-memory.md)）**：`Scheduler.submit` 改为 `async def submit(job, channel)`——task 内容由 `Job` 封装，不再持 mode/state，Scheduler 只调度。`CloudEngine.evolve` 经 `JobFactory.get_job(JobType.EVOLVE, scope, mode=mode)` 取实例 + `await scheduler.submit(job, channel)` 提交。**注意 profile-aware evolve 当前未解决**：`EvolveJobSpec.with_scope` 仍只持 default Evolver，未支持运行时覆盖入参 `evolver=`——多 profile evolve 场景下 Job 仍用 default evolver，是已知遗留（见 F06 已知遗留）。`write` 路径 `infer=true + middle=true` 子分支同款经 JobFactory 取 `MiddleToLongJob` 实例 + 经 `AsyncTimerScheduler` per scope TimerWheel 周期触发；多 profile 适配时 CloudEngine 通过 `get_job(evolver=, index=)` 运行时覆盖入参注入 binding 的（保证 Job 内部的 evolver/index 与原文落盘时一致），详见 F06 决策 4。
- 索引层仍可能保存明文摘要、文本、向量或图节点属性。KV 加密只能保护真源与 KV value，不等于全链路加密。
- `message_type` 是否升为公开 API 字段，需要在 `S02-memory-api` 中单独固化；第一阶段可以通过 metadata 兼容落地。
- Space 管理面接口、space policy 存储、用量统计和 offboarding 不是 CloudEngine 的职责，已由 `SpaceManager` 控制算子承接；CloudEngine 只消费已解析后的 target scope 与 pipeline/profile 配置。
- SpacePolicy 除 `principal_path` 外尚未接入执行路径；`require_space`、retention、quota、
  index/pipeline profile 与 storage strategy 需要各自的策略应用点和验证测试。
