# F02 — Assets 映射与 Normalizer 能力边界

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-05 |
| 影响范围 | `RawPayload` / Ingestor / Normalizer / InMemoryEngine / CloudEngine / 入口错误映射 |
| 测试基线 | Normalizer、视频入口、批量写入及 CloudEngine 定向回归 59 passed；全量 unit 仅余 2 个既有 entity_linker 日志捕获失败 |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

## 背景

`MemoryEngine.write` 接收 assets，但旧流程没有把它们交给 Ingestor，而是在 ingest 完成后统一回填到首个 Segment。这使 Engine 隐式假设了 Ingestor 的产出结构，自定义 Ingestor 即使已经按自身策略完成 assets 映射，结果仍会被 Engine 二次改写。

## 决策

1. `RawPayload` 增加 `assets: list[str]`，作为从调用边界传入 Ingestor 的资产引用载体。
2. InMemoryEngine 与 CloudEngine 只对 write 入参做防御性复制，并将副本放入 `RawPayload.assets`。
3. Ingestor 拥有 assets 到 `MemoryUnit.segments` 的映射责任。Engine 在 ingest 返回后不再新建 Segment、不再回填首 Segment、不再改写任何 `Segment.assets`。
4. 当前 `SimpleIngestor` 继续产出单 MemoryUnit/单 Segment，并将一个 payload 的全部 assets 复制到该 Segment。这是具体实现行为，不上升为 Ingestor 接口的数量契约。
5. `Normalizer.modalities()` 是“是否支持该模态”的单一能力声明，不负责决定原始数据应放入 `RawPayload.data` 还是 `RawPayload.uri`。`RoutingNormalizer` 在构造时校验显式 route 的 delegate，配置矛盾抛 `ValidationError`。
6. Ingestor 在 normalize 前校验真实 payload；不支持时抛含 capability/value/component 的 `UnsupportedCapabilityError`，不产生 MemoryUnit、不落盘、不建索引。HTTP 视频入口缺少视频 Normalizer 时使用同一能力错误；已配置 Normalizer 但缺少 video Evolver 时仍按装配错误抛 `ValidationError`。
7. PassthroughNormalizer 只声明支持已经是 UTF-8 文本表示的 TEXT/CODE：CODE 不读取源码文件，只有 TEXT 保留无 data 时的 URI 兼容回退。DOCUMENT 需要专用解析型 Normalizer；IMAGE/AUDIO/VIDEO 同样不得把 URI 当作普通 content。UTF-8 解码失败按非法文本投影抛 `ValidationError`。
8. 不在 Engine/API 维护模态支持列表，也不增加 assets 数量限制或 payload/MemoryUnit/Segment 数量限制；当前 Engine 的载体字段映射限制单独记录为已知遗留。

## 拒绝的方案

- **继续由 Engine 回填首 Segment**：拒绝。Engine 不了解 Ingestor 的分段策略，首段假设会覆盖或污染自定义映射结果。
- **在接口上固定 `1 RawPayload -> 1 MemoryUnit -> 1 Segment`**：拒绝。当前 SimpleIngestor 的最小实现不应限制后续分块、多模态或聚合型 Ingestor。
- **在 Engine/API 硬编码全局模态允许列表**：拒绝。支持集合由可插拔 Normalizer 自身声明；原始数据的载体字段选择是另一份契约，不与支持集合混为一谈。

## 验证

- SimpleIngestor 用例验证 payload assets 被等值复制到当前 Segment，且不共享可变 list 实例。
- InMemoryEngine 与 CloudEngine 分别注入“把 assets 放入第二个 Segment”的测试 Ingestor，验证 Engine 传入 assets 且保留 Ingestor 的分段结果。
- 错误 `video -> passthrough` route 在直接构造与 `build_kernel()` 装配中均失败。
- SimpleIngestor、PassthroughNormalizer、RoutingNormalizer 和 VideoNormalizer 覆盖统一的运行时能力错误；HTTP 与 CLI 在直接调用 `MemoryAPI` 的协议边界通过共享错误映射返回 400。
- TEXT URI 兼容回退和 UTF-8 CODE 文本直通继续通过；DOCUMENT 及媒体模态不会由 passthrough 当作普通 content，非法 UTF-8 文本转为 `ValidationError`。

## 已知遗留

- `RawPayload.assets` 当前仅是 `list[str]` 引用，不表达资产类型、存储状态或与 Segment 的结构化关系。
- 当前只有 SimpleIngestor；多 MemoryUnit/多 Segment 的具体映射能力交给后续 Ingestor 实现，不在本次预先固化。
- `MemoryEngine.write` 当前只对 `Modality.VIDEO` 把 `content` 写入 `RawPayload.uri`，其他模态写入 `RawPayload.data`。因此 `modalities()` 只决定“能不能进”，不能解决“数据放哪里”；新增其他外部 URI 模态前，需要单独补充显式的 data/uri 载体契约。
- 当前没有 DocumentNormalizer；PDF/Office 等文档原件不能通过 passthrough 解析，需在专用实现落地后再声明 DOCUMENT 能力。
