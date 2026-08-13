# Agent Memory Ingest（接入层）

**规约文档**：[S01-ingest-access.md](../../docs/specs/S01-ingest-access.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

承接多模态信息源，保留原模态资产引用（`MemoryUnit.assets`），规约出可治理文本投影（`content`）。**接入层不落盘**——产出 `MemoryUnit` 后，写入存储由构建层完成。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | IngestOperator 基类：所有接入层算子的自描述契约（operator_type / health） |
| `source.py` | Source 接口：多模态信息源连接器（fetch 拉取原始数据 → RawPayload） |
| `ingestor.py` | Ingestor 接口：编排 Source → Normalizer → 组装 MemoryUnit |
| `source_impl/` | Source 实现目录 |
| `source_impl/text_source.py` | TextSource：纯文本信息源（当前唯一实现） |
| `bootstrap.py` | 统一触发 Source 实现注册 |

## 行为铁律

1. **接入层不落盘**  
   `Ingestor.ingest` 返回 `list[MemoryUnit]` 后，真源写入与索引构建全部由 `construction` 层调用 `storage` 完成。本层禁止 import storage。

2. **投影可重建**  
   `MemoryUnit.content` 是 `MemoryUnit.assets` 的可重建派生。换了更好的规约模型（OCR/ASR/caption）后，拿原件重跑 Normalizer 即可重建投影。

3. **Source 只拉不规约**  
   `Source.fetch` 只产出 `RawPayload`（原始二进制 + 元数据），不做格式转换。规约（多模态 → 文本）归 `common/Normalizer`，编排归 `Ingestor`。

4. **Ingestor 不调用 LLM**  
   如需 caption/ASR/摘要等，由注入的 Normalizer 插件内部调用 LLM。Ingestor 只负责编排流水线。

## 规约投影（Normalization）

**什么是规约投影**：把各种格式的来源统一转成一份系统能处理的文字描述。

| 模态 | 规约方式 |
|------|----------|
| 图片 | 图片描述（caption）+ 图中文字（OCR） |
| 音频 | 语音转写文字稿（ASR） |
| 视频 | 关键画面描述 + 字幕/转录 |
| 文档/代码 | 解析出的正文 |
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
2. Source 实现通过 `@SourceProducer.register("name")` 自注册。
3. Ingestor 接收 `list[RawPayload]`，返回 `list[MemoryUnit]`，不做持久化。
4. `source_impl/__init__.py` import 所有实现模块触发注册。
