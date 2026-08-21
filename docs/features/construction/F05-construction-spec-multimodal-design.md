# F05 — 多模态记忆接入设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-05 |
| 影响范围 | `bootstrap/core/`、`jiuwen_memory/control/`、`jiuwen_memory/ingest/`、`jiuwen_memory/construction/`、`jiuwen_memory/retrieval/` |
| 状态 | **已落地**（视频记忆写入、异步任务管理及多通道检索） |

## 背景

多模态记忆系统将视频转换为两级记忆：CLM 表示可定位的片段级内容，ELM 表示由多个
CLM 组成的事件级摘要。两级结果统一构建为 mem2.0 原生 `MemoryUnit`，复用原有写入、
存储、索引和检索链路。

核心技术包括：

1. **自适应双流事件边界分割**：联合 ASR 语义变化和视觉场景变化识别事件边界。
2. **多层级记忆单元离线构建**：根据分割结果构建片段级 CLM 和事件级 ELM。

## 总体链路

```text
视频请求
  -> Bootstrap 接口适配
  -> Control 创建后台任务
  -> MemoryAPI.add()
  -> Ingest 视频规约
  -> Construction 构建 CLM/ELM
  -> 原生 KVStore 与 IndexBuilder
  -> MemoryAPI.search()
```

后台任务只异步执行一次 `MemoryAPI.add()`。视频处理和 CLM/ELM 构建位于原生 Engine write
链路内部，普通文本请求不进入视频分支。

## 模块实现

| 模块 | 代码路径 | 职责 |
|---|---|---|
| 接口适配 | `bootstrap/core/handler.py`、`bootstrap/core/server.py` | 识别视频请求、校验参数并组织响应 |
| 后台任务管理 | `jiuwen_memory/control/ingest_job.py`、`jiuwen_memory/control/job_impl/ingest_job.py` | 定义任务管理接口并实现异步执行、状态和提交幂等 |
| 视频规约 | `jiuwen_memory/common/normalizer/normalizer_impl/` | 执行视频处理并生成 clips/events 结构化结果 |
| 多层级记忆构建 | `jiuwen_memory/construction/extractor_impl/video_memory_extractor.py` | 将 clips/events 转换为 CLM/ELM `MemoryUnit` |
| 多模态记忆检索 | `jiuwen_memory/retrieval/retriever_impl/multimodal_retriever.py` | 并行召回原生记忆、CLM 和 ELM 后融合 |
| 多模态配置 | `examples/config_multimodal.yml` | 声明视频 Normalizer、Evolver、Retriever 及其参数 |

### 1. 接口适配

**代码路径**：`bootstrap/core/handler.py`、`bootstrap/core/server.py`

**输入输出**：输入视频 URI、scope、`payload_id`、`system_metadata` 和
`user_metadata`；输出 `ing_` 前缀的`job_id`，由 `/v1/job` 返回后续状态和记忆结果。

**职责**：`handler.py` 识别 `modality=video`，校验请求并把原生写入调用交给 Control；
`MemoryAPI` 装配过程创建 `IngestJobController`，`server.py` 只持有该接口并在服务关闭时
调用 `close()`。Bootstrap 不实现线程池、任务状态或幂等规则。

核心调用：

```python
submission = srv.ingest_jobs.submit(
    payload_id=payload_id,
    source_ref=uri,
    scope=scope,
    task=lambda: srv.api.add(
        uri,
        scope,
        Modality.VIDEO,
        identity=identity,
        assets=assets,
        system_metadata=system_metadata,
        user_metadata=user_metadata,
    ),
)
```

### 2. 后台任务管理

**代码路径**：`jiuwen_memory/control/ingest_job.py`、`jiuwen_memory/control/job_impl/ingest_job.py`

**输入输出**：输入 `payload_id`、视频 URI、scope 和待执行任务；输出 `IngestSubmission`，并维护 `pending -> running -> succeeded/failed` 状态。

**职责**：接口 `IngestJobController` 定义任务管理契约；实现
`InProcessIngestJobController` 管理线程池、队列容量、任务状态、幂等和状态持久化。
任务记录与 `payload_id -> job_id` 映射分别保存在 KVStore 的 `/ingest/jobs/` 和
`/ingest/payloads/` 命名空间。

```python
future = self._executor.submit(self._run, job.id, task)
```

| 场景 | Control 行为 |
|---|---|
| 首次提交 | 创建后台任务并立即返回 `job_id` |
| 相同 scope、`payload_id` 和 URI | 复用未失败任务 |
| 相同 `payload_id` 指向不同 URI | 返回冲突 |


### 3. 视频规约

**代码路径**：

- `jiuwen_memory/common/normalizer/normalizer_impl/routing_normalizer.py`
- `jiuwen_memory/common/normalizer/normalizer_impl/video_asr.py`
- `jiuwen_memory/common/normalizer/normalizer_impl/video_normalizer.py`
- `jiuwen_memory/common/normalizer/normalizer_impl/video_pipeline.py`

**输入输出**：输入 `RawPayload(modality=video, uri=...)`；输出一段结构化 JSON 文本，
其中包含视频 URI、clips、events 和时间范围。该文本暂存在源 `MemoryUnit.content` 中，
供 Construction 解析，不作为最终记忆写入。

**职责**：`RoutingNormalizer` 仅按模态选择 Normalizer；`VideoNormalizer` 调用视频流水线，
完成 ASR/视觉双流处理、事件边界确认以及 CLM/ELM 原始结果生成。

ASR 通过 `asr_port` 注入独立的 `VideoAsrService`，由远程服务返回带时间戳的转写片段。

```python
clips, events = self._extract_video_memory(payload)
return json.dumps({
    "payload_id": payload.id,
    "asset_uri": payload.uri,
    "clips": clips,
    "events": events,
})
```

流水线使用任务级临时目录处理音频、帧和模型输入，任务结束后自动删除；不保存
`short_term.json`、`medium_term.json` 等中间结果文件。

### 4. 多层级记忆构建

**代码路径**：

- `jiuwen_memory/construction/extractor_impl/video_memory_extractor.py`
- `jiuwen_memory/construction/evolver_impl/orchestrating_evolver.py`（原生实现）

**输入输出**：输入视频规约产生的源 `MemoryUnit`；输出可直接落盘的 CLM/ELM
`MemoryUnit` 列表。

**职责**：

- `VideoMemoryExtractor` 在一次 `extract()` 中先将 clips 转换为 CLM，再根据 events
  生成 ELM；CLM/ELM 的 `provenance` 均回指视频源 MemoryUnit，ELM 通过稳定的片段源 ID记录所含 CLM。
- 视频 pipeline 选择原生 `OrchestratingEvolver`；它在 `EvolveMode.EXTRACT` 下调用
  `VideoMemoryExtractor`，并复用原生去重、保存和索引逻辑。

核心构建逻辑：

```python
extracted = self._extractor.extract(units, context=context)
result = self._dedup_batch(extracted)
```

`system_metadata.infer=true` 时，`MemoryEngine.write()` 在 Ingest 后调用
`evolver.evolve(units, EvolveMode.EXTRACT)`。因此一次视频提交只调用一次
`MemoryAPI.add()`，CLM/ELM 不逐条重新进入 Engine write。

### 5. 存储与索引

**代码路径**：原生 `KVStore`、`IndexBuilder` 及其当前配置实现。

**职责**：最终 CLM/ELM 使用 `/memory/<MemoryUnit.id>` 保存，并由原生全文和向量
IndexBuilder 建立索引。系统不新增多模态 Store，原始视频不复制，只在
`segments[].assets` 中保留 URI。

服务配置文件为 `examples/config_multimodal.yml`，配置完成后可按以下方式启动：

```bash
./scripts/run-server.sh --host 127.0.0.1 --port 8002 examples/config_multimodal.yml
```

视频流水线通过 `asr_port`、`llm_port` 和 `vlm_port` 分别注入语音、文本和视觉模型；
`asr_port` 是具名实例引用，具体参数见 `examples/config_multimodal.yml`。

### 6. 记忆检索

**输入输出**：输入自然语言 query、scope 和返回数量；输出原生文本记忆、相关视频片段
记忆和事件记忆的融合结果。用户不需要提供 `video_id`。

**职责**：检索仍统一通过 `MemoryAPI.search()`，不新增多模态检索接口。
`MultimodalRetriever` 同时检索原生记忆、片段级 CLM 和事件级 ELM，各通道独立返回
结果后统一融合。检索前不扫描 KV 判断 scope 是否存在多模态记忆；未构建视频记忆时，
CLM 和 ELM 分支自然返回空结果，融合结果等价于只检索原生记忆。

目标链路：

```text
MemoryAPI.search(query, scope)
  -> MultimodalRetriever
     -> 原生文本记忆召回 -> text_hits
     -> 按 system_metadata.modal_type=multimodal、memory_level=clm 召回 CLM
     -> 按 system_metadata.modal_type=multimodal、memory_level=elm 召回 ELM
     -> 融合 text_hits、clip_hits 和 event_hits
     -> RRF 去重融合与 top_k 截断
```

CLM 和 ELM 两个检索通道都复用原生关键词、向量、融合和重排能力。CLM 用于定位具体
视频片段，ELM 用于召回完整事件语义；两路互不作为前置条件，分别召回后再统一融合。

| 参数 | 含义 |
|---|---|
| `event_top_k` | ELM 通道最多保留的结果数量 |
| `clip_top_k` | CLM 通道最多保留的结果数量 |
| `top_k` | 融合后最终返回数量 |

## 记忆格式

文本与视频记忆使用同一个 `MemoryUnit`。视频差异主要体现在 `segments`、`system_metadata`
和 `provenance`。

| 字段 | 文本记忆 | 视频 CLM/ELM |
|---|---|---|
| `segments[].content` | 原始文本或文本规约结果 | 片段描述或事件摘要 |
| `segments[].source` | `text` | `video` |
| `segments[].assets` | 通常为空 | 原始视频 URI |
| `system_metadata.modal_type` | 无固定值 | `multimodal` |
| `system_metadata.memory_level` | 无固定值 | `clm` 或 `elm` |
| `provenance` | 由原生构建流程确定 | CLM/ELM 均记录视频源 MemoryUnit ID |

多模态系统元数据：

| 字段 | 含义 |
|---|---|
| `modal_type` | 固定为 `multimodal` |
| `memory_level` | `clm` 或 `elm` |
| `video_id` | 视频规约中的 `payload_id` |
| `source_memory_id` | 视频流水线中的片段 ID 或事件 ID |
| `child_clm_source_ids` | 仅 ELM 使用，记录事件包含的片段源 ID 列表 |
| `start_seconds` | 对应原视频的开始时间，浮点秒 |
| `end_seconds` | 对应原视频的结束时间，浮点秒 |

CLM 逻辑格式示例：

```json
{
  "id": "<memory-unit-id>",
  "scope": {"org": "tenant-a", "user": "user-001", "agent": "", "session": ""},
  "tier": "episodic",
  "layers": {"l0": "", "l1": ""},
  "segments": [{
    "content": "Visual summary: ...\nSpeech transcript: ...",
    "assets": ["file:///data/demo.mp4"],
    "source": "video"
  }],
  "source_ref": "video-001",
  "provenance": ["<video-source-memory-unit-id>"],
  "tags": [],
  "system_metadata": {
    "modal_type": "multimodal",
    "memory_level": "clm",
    "video_id": "video-001",
    "source_memory_id": "clip-001",
    "start_seconds": 0.0,
    "end_seconds": 30.0
  },
  "user_metadata": {},
  "lifecycle": "active"
}
```

CLM `content` 已是视觉片段描述，ELM `content` 已是事件摘要，当前不再额外生成 L0/L1
精简文本，在 `system_metadata` 下的 `memory_level` 里标注是 `clm` 还是 `elm`。

## 当前遗留

- 图片、音频和 Dreaming 闲时扫描尚未实现。
- 查询驱动的快速处理尚未实现。对于网页视频链接或上传后立即提问的场景，可先使用
  VLM 做粗粒度处理，同时在后台完成完整记忆构建。
