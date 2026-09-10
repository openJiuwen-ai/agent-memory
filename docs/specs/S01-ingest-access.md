# S01 — 接入层（Ingest & Access Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/ingest/ |
| 最近一次修订日期 | 2026-09-10 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联资产流程特性 | docs/features/ingest/F02-assets-ingestor-boundary.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/common/F08-memory-tree.md |

## Metadata 转换契约

`RawPayload` 与 `MemoryUnit` 都以 `system_metadata` / `user_metadata` 承载双命名空间。
Ingestor 分别复制两个 dict，不得合并或在两者之间 fallback。系统命名空间中的四个
层级叶提示按下文映射到 `MemoryUnit.hierarchy` 后移除；其余系统键继续透传。
`user_metadata` 不解释，即使包含同名 `hierarchy_` 键也不参与结构映射。

## 范围 / 边界

**管什么**：
- 多模态信息源连接（对话/文档/代码/工具轨迹/图像/音视频/外部导入）
- 原始数据拉取（产出 `RawPayload`）
- 规约投影：把多模态原始数据统一翻译为可治理文本 `content`（OCR/ASR/caption/解析），原模态资产保留在 `assets`
- 编排整条接入流水线（Source → Normalizer → 组装 MemoryUnit）

**不管什么**：
- 不负责落盘（真源写入由构建层调用 `jiuwen_memory/storage` 完成）
- 不做 LLM 调用（如需 caption/ASR 等，由注入的 Normalizer 插件内部调用）
- 不做分类/索引/演进（由构建层负责）
- 不做鉴权（由 `jiuwen_memory/api` 层在入口执行）

## 不变量

1. **接入层不落盘**：`Ingestor.ingest` 返回 `list[MemoryUnit]` 后，写入真源与索引全部由控制层/构建层完成。
2. **投影可重建**：`content` 是 `assets` 的可重建派生；换了更好的规约模型后，拿原件重跑 Normalizer 即可重建。
3. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 Producer 工厂被外部装配消费。
4. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `IngestOperator`，自描述 + 存活探测。
5. **Source 只拉不规约**：Source 只产出 `RawPayload`，不做格式转换——规约归 Normalizer。
6. **资产映射归 Ingestor**：调用方通过 `RawPayload.assets` 传入资产引用，Ingestor 决定如何将其映射到产出的 `MemoryUnit.segments`；Engine 不得在 ingest 返回后改写 `Segment.assets`。
7. **不固定数量关系**：契约不规定一个 `RawPayload` 必须产生几个 `MemoryUnit` 或 `Segment`，也不规定资产引用必须分配给某个固定 Segment；具体策略由 Ingestor 实现定义。
8. **模态声明是能力边界**：Ingestor 调用 `normalize` 前必须确认 `RawPayload.modality` 在 `Normalizer.modalities()` 中；不支持时抛 `UnsupportedCapabilityError`，不得读取 data/uri、产生 MemoryUnit 或触发后续写入。RoutingNormalizer 中显式 route 与 delegate 的能力矛盾属于装配错误，在构造时抛 `ValidationError`。

## 接口契约

### IngestOperator（基类，`base.py`）

```python
class IngestOperatorType(str, Enum):
    SOURCE / INGESTOR

class IngestOperator(ABC):
    def operator_type(self) -> IngestOperatorType  # 自描述
    def health(self) -> None                       # 存活探测：健康返回 None，否则抛异常
```

### Source（`source.py`）

信息源连接器，对接一类外部数据源，把源数据拉取为统一的 `RawPayload`。

| 方法 | 签名 | 语义 |
|------|------|------|
| `modalities` | `() -> list[Modality]` | 返回本信息源会产出的模态类型 |
| `fetch` | `(since: datetime \| None = None) -> list[RawPayload]` | 拉取原始数据；`since` 非空时增量拉取该时间点之后的新数据 |

### Ingestor（编排算子，隐含接口）

编排 Source → Normalizer → 组装 MemoryUnit 的完整流水线。

| 方法 | 签名 | 语义 |
|------|------|------|
| `ingest` | `(payloads: list[RawPayload]) -> list[MemoryUnit]` | 对一批原始负载做规约投影，组装为记忆单元返回 |

**ingest 路径**：
```
Source.fetch() → list[RawPayload]
→ 校验 payload.modality 属于 Normalizer.modalities()
→ 校验并提取 system_metadata 中的层级叶提示（如有）
→ Normalizer.normalize(payload) → content 文本投影
→ Ingestor 组装 MemoryUnit，并将 payload.assets 映射到产出的 Segment
→ 返回 list[MemoryUnit]
```

## 数据结构

### RawPayload（`jiuwen_memory/common/type_def/raw.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | str | 原始负载唯一 id |
| `scope` | Scope | 归属 scope |
| `modality` | Modality | 来源模态 |
| `data` | bytes | 原始二进制内容 |
| `uri` | str | 外部来源 URI/路径 |
| `system_metadata` | dict[str, MetadataValueType] | 系统控制与内部状态元数据 |
| `user_metadata` | dict[str, MetadataValueType] | 用户自定义元数据，接入链路只透传 |
| `occurred_at` | datetime \| None | 事件发生时间 |
| `assets` | list[str] | 待 Ingestor 映射到产出 Segment 的资产引用；列表长度不隐含 MemoryUnit/Segment 数量 |

### 层级叶提示（阶段 1 已实现）

接入侧只接受 `RawPayload.system_metadata` 中下列四个键，不接收任意父子边：

| 键 | 类型 | 语义 |
|---|---|---|
| `hierarchy_kind` | str | `time` / `topic` / `directory` / `cluster` / `custom` |
| `hierarchy_role` | str | TIME 只接受 `snapshot`；其他 kind 只接受 `node` |
| `hierarchy_span_start` | ISO 8601 str | 结构覆盖区间起点 |
| `hierarchy_span_end` | ISO 8601 str | 结构覆盖区间终点 |

kind/role 必须同时出现；区间必须成对且起点不晚于终点，TIME 必须带区间，其他 kind
可以省略。无提示时使用空 `HierarchyRef`。有效提示只填充 kind、role 和 span，
父子引用保持空、结构状态为 `ACTIVE`，不生成父节点。

未知 `hierarchy_` 前缀键（包括父子边提示）、非法枚举、父侧角色、无效时间或不完整
提示均抛 `ValidationError`。已消费的四个键不再留在产出 unit 的 `system_metadata`
中；不得由 Engine 在接入返回后重新回注。输入 payload 的两个 metadata dict 不被修改。

叶提示不是索引投影的任意写入口。仅索引拥有的 `hierarchy_status`、`parent_id`、
`span_start`、`span_end` 已列入系统保留键，公开 add/update 在既有参数校验中拒绝
调用方赋值；业务同名字段应放 `user_metadata`，仍原样透传，不接受后再静默删除。
`hierarchy_kind` / `hierarchy_role` 兼作合法叶提示，不能一并禁用；接入时间提示是
`hierarchy_span_start/end`，与索引的 `span_start/end` 不是同一组输入键。

本阶段的叶提示解析自行检查字段，不调用 `validate_ref` / `validate_tree`，也不查询
存储或验证全树；这两个纯函数的边界见 S07。普通 `infer` 分流保持不变，不保证所有
Extractor 都把源 unit 的 hierarchy 继承到新派生 unit，不能把“提示接入成功”视为
“infer 后派生记忆一定保留叶身份”。

### Modality（`jiuwen_memory/common/type_def/memory.py`）

```
TEXT / IMAGE / AUDIO / VIDEO / CODE / DOCUMENT
```

枚举值只描述来源模态，不代表默认 Normalizer 已具备对应的规约能力。当前
`PassthroughNormalizer` 只接收已经是 UTF-8 文本的 TEXT/CODE：CODE 表示调用方已经提供
源码文本，不读取源码文件；DOCUMENT（PDF/Office 等原件）需要专用解析型 Normalizer。
`Normalizer.modalities()` 只决定是否支持该模态，不决定数据使用 `RawPayload.data` 还是
`RawPayload.uri`。

## 实现注册机制

`Source` 和 `Ingestor` 的装配方式不同：

- `Source` 实现不走 Producer，由信息源接入侧按连接参数直接构造并调用；
- `Ingestor` 实现通过 `@IngestorProducer.register("name")` 自注册，由
  `ingest.bootstrap.register_ingestors` 统一触发实现模块导入。

```
jiuwen_memory/ingest/source_impl/
    __init__.py             # 重导出 Source 实现类
    <impl_class_snake>.py   # Source 具体实现，由调用方直接构造

jiuwen_memory/ingest/ingestor_impl/
    __init__.py             # 导入实现模块，触发 Ingestor 注册
    <impl_class_snake>.py   # Ingestor 具体实现 + @IngestorProducer.register("name")
```


## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S02-memory_api | MemoryAPI.add 触发控制层→本层的 Ingestor.ingest |
| S05-construction | 构建层接收本层产出的 MemoryUnit 做落盘+索引+演进 |
| S07-common | Normalizer/Tokenizer 等共享插件由本层消费 |
| architecture.md §10 | 多模态信息源接入与规约投影 |

## 修订记录

| 日期 | 内容 |
|---|---|
| 2026-09-10 | 同步 F08 阶段 1：系统元数据叶提示映射、拒绝边/父角色、消费后不回注及 infer 继承边界；不引入建树或落盘职责 |
