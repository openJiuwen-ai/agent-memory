# Agent Memory Ingest（接入层）

**规约文档**：[S01-ingest-access.md](../../docs/specs/S01-ingest-access.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

承接多模态信息源，保留原模态资产引用（`Segment.assets`），规约出可治理文本投影（`content`）。**接入层不落盘**——产出 `MemoryUnit` 后，写入存储由构建层完成。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | IngestOperator 基类：所有接入层算子的自描述契约（operator_type / health） |
| `source.py` | Source 接口：多模态信息源连接器（fetch 拉取原始数据 → RawPayload） |
| `hierarchy_hints.py` | 纯叶提示解析与消息时间补齐；不读策略或存储，拒绝边与父角色 |
| `ingestor.py` | Ingestor 接口：编排 Source → Normalizer → 组装 MemoryUnit |
| `ingestor_impl/simple_ingestor.py` | SimpleIngestor：规约后按 `max_unit_chars` 预切分——超限文本产出多个 MemoryUnit（每 unit 单 Segment，`system_metadata` 记 `_part`/`_parts` 序号），未超限产出单个；该 payload 的全部 assets 复制到每个 Segment |
| `source_impl/` | Source 实现目录 |
| `source_impl/text_source.py` | TextSource：纯文本信息源（当前唯一实现） |
| `bootstrap.py` | 统一触发 Ingestor 实现注册 |

## 行为铁律

0. **双 metadata 分离转换**
   `RawPayload.system_metadata` / `user_metadata` 分别复制到 `MemoryUnit`，不合并、
   不彼此 fallback。系统侧四个 hierarchy 叶提示消费为结构，用户侧同名键原样保留。

1. **接入层不落盘**  
   `Ingestor.ingest` 返回 `list[MemoryUnit]` 后，真源写入与索引构建全部由 `construction` 层调用 `storage` 完成。本层禁止 import storage。

2. **投影可重建**  
   `MemoryUnit.content` 是 `MemoryUnit.assets` 的可重建派生。换了更好的规约模型（OCR/ASR/caption）后，拿原件重跑 Normalizer 即可重建投影。

3. **Source 只拉不规约**  
   `Source.fetch` 只产出 `RawPayload`（原始二进制 + 元数据），不做格式转换。规约（多模态 → 文本）归 `common/Normalizer`，编排归 `Ingestor`。

4. **Ingestor 不调用 LLM**  
   如需 caption/ASR/摘要等，由注入的 Normalizer 插件内部调用 LLM。Ingestor 只负责编排流水线。

5. **Ingestor 拥有资产映射责任**
   `RawPayload.assets` 是输入资产引用；具体 Ingestor 决定其在产出 `MemoryUnit.segments` 中的位置。不把 RawPayload、MemoryUnit、Segment 之间的数量关系写成接口不变量。当前 `SimpleIngestor` 把 payload 的全部 assets 防御性复制到它产出的每个 Segment：超限切分时 N 片各带全量资产引用（复制的是引用而非资产本体，N 倍引用放大是 simple 实现的既定映射策略——分片可独立检索与披露，不维护分片→资产的精确子集映射）；这是 `simple` 实现行为，不是 Ingestor 接口的数量契约。

6. **Ingestor 在规约前执行模态门禁**
   调用 `Normalizer.normalize` 前先以 `Normalizer.modalities()` 校验当前 `RawPayload.modality`；不支持时抛 `UnsupportedCapabilityError`，不产生 MemoryUnit、不进入落盘和建索引。

## 规约投影（Normalization）

**什么是规约投影**：把各种格式的来源统一转成一份系统能处理的文字描述。

| 模态 | 规约方式 |
|------|----------|
| 图片 | 图片描述（caption）+ 图中文字（OCR） |
| 音频 | 语音转写文字稿（ASR） |
| 视频 | 关键画面描述 + 字幕/转录 |
| 代码 | 已是 UTF-8 的源码文本可直通；代码文件需解析型 Normalizer |
| 文档 | PDF/Office 等原件由专用 Normalizer 解析正文 |
| 对话 | 基本为原文，做整理 |

收益：下游分词/向量化/建索引/检索只处理 `content` 这一份文字，不必关心记忆原来是图还是录音。

## 与其他子目录的边界

**本模块管**：
- 多模态信息源连接（Source）
- 原始数据拉取（fetch → RawPayload）
- 规约投影编排（Ingestor：Source → Normalizer → MemoryUnit）

**不管**：
- 落盘（归 `construction`）
- 分类/索引/演进（归 `construction`）
- LLM 调用（Normalizer 插件内部实现，归 `common`）
- 鉴权（归 `api`）

## 本地约束

1. 所有 Operator 必须实现 `operator_type()` 和 `health()`（继承自 `IngestOperator`）。
2. Source 实现不走 Producer，由信息源接入侧按连接参数直接构造。
3. Ingestor 接收 `list[RawPayload]`，返回 `list[MemoryUnit]`，不做持久化。
4. Ingestor 实现通过 `@IngestorProducer.register("name")` 自注册，`ingestor_impl/__init__.py` import 实现模块触发注册。
5. 当前 `SimpleIngestor` 把一个 payload 的全部 assets 防御性复制到其产出的单 Segment；这是 `simple` 实现行为，不是 Ingestor 接口的数量契约。
6. `SimpleIngestor` 在任何 Normalizer 调用前执行 `ensure_normalizer_supports`；自定义 Normalizer 只需正确实现 `modalities()` 即可参与同一门禁。
7. 默认 `PassthroughNormalizer` 只接已经是 UTF-8 文本的 TEXT/CODE；DOCUMENT 原件必须配置专用解析型 Normalizer，不能把文件 URI 当作解析后的正文。

8. `SimpleIngestor` 在 normalize 前校验并消费叶提示，不创建父子边；无提示保持空结构。
9. 预切分 `_part`/`_parts` 是 `SimpleIngestor` 的内部序号约定，不对输入开放：payload `system_metadata` 携带的同名键一律剔除，多片时强制覆盖为真实序号，单片时不残留。`max_unit_chars` 缺省 8000；显式 null/空串/非整数在装配期显式拒绝（`or 0` 式兜底会静默关闭切分），0/负数是文档化的关闭开关。段落聚合路径 `"\\n\\n".join(pieces)` 严格还原原文；单段超长硬切兜底，硬切点拼接引入一个段落分隔（不影响记忆语义）。设计取舍见 [F08](../../docs/features/F08-large-content-guardrails.md)。
10. `enrich_snapshot_hints` 由 Engine 传入开关；补齐只用 occurred_at，完整提示优先。
   无 occurred_at 时保持原输入；显式 TIME/Snapshot 单边区间不自动修补，已有端点原样保存。
   开启时缺时不使写入失败，非法身份、时间格式、逆序区间或边提示仍拒绝。
   关闭时只移除四个合法叶提示，不补齐时间；未知结构键和边提示仍拒绝，原输入不修改。

11. 叶区间比较复用 common/time_utils.as_utc；补齐与解析保留输入时间表示。
