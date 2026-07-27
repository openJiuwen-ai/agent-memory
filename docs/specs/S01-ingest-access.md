# S01 — 接入层（Ingest & Access Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/ingest/ |
| 最近一次修订日期 | 2026-07-27 |

| 关联特性文档 | docs/features/F01-system-spec-design.md |
## 范围 / 边界

**管什么**：
- 多模态信息源连接（对话/文档/代码/工具轨迹/图像/音视频/外部导入）
- 原始数据拉取（产出 `RawPayload`）
- 规约投影：把多模态原始数据统一翻译为可治理文本 `content`（OCR/ASR/caption/解析），原模态资产保留在 `assets`
- 编排整条接入流水线（Source → Normalizer → 组装 MemoryUnit）

**不管什么**：
- 不负责落盘（真源写入由构建层调用 `src/storage` 完成）
- 不做 LLM 调用（如需 caption/ASR 等，由注入的 Normalizer 插件内部调用）
- 不做分类/索引/演进（由构建层负责）
- 不做鉴权（由 `src/api` 层在入口执行）

## 不变量

1. **接入层不落盘**：`Ingestor.ingest` 返回 `list[MemoryUnit]` 后，写入真源与索引全部由控制层/构建层完成。
2. **投影可重建**：`content` 是 `assets` 的可重建派生；换了更好的规约模型后，拿原件重跑 Normalizer 即可重建。
3. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 Producer 工厂被外部装配消费。
4. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `IngestOperator`，自描述 + 存活探测。
5. **Source 只拉不规约**：Source 只产出 `RawPayload`，不做格式转换——规约归 Normalizer。

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
→ Normalizer.normalize(payload) → content 文本投影
→ 组装 MemoryUnit（content/assets/source/temporal 等）
→ 返回 list[MemoryUnit]
```

## 数据结构

### RawPayload（`common/type_def/raw.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | str | 原始负载唯一 id |
| `scope` | Scope | 归属 scope |
| `modality` | Modality | 来源模态 |
| `data` | bytes | 原始二进制内容 |
| `uri` | str | 外部来源 URI/路径 |
| `metadata` | dict[str, Any] | 附加元数据；JSON 标量原生类型由接入链路透传 |
| `occurred_at` | datetime \| None | 事件发生时间 |

### Modality（`common/type_def/memory.py`）

```
TEXT / IMAGE / AUDIO / VIDEO / CODE / DOCUMENT
```

## 实现注册机制

```
src/ingest/source_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @SourceProducer.register("name")
```


## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S02-memory_api | MemoryAPI.write 触发控制层→本层的 Ingestor.ingest |
| S05-construction | 构建层接收本层产出的 MemoryUnit 做落盘+索引+演进 |
| S07-common | Normalizer/Tokenizer 等共享插件由本层消费 |
| architecture.md §10 | 多模态信息源接入与规约投影 |
