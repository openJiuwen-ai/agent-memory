# F01 — 接入层实现规约（jiuwen_memory/ingest/*_impl）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-02 |
| 影响范围 | jiuwen_memory/ingest/{ingestor,source}_impl/，docs/specs/S01-ingest-access.md（如有） |
| 测试基线 | `pytest tests/unit/ingest/test_simple_ingestor.py tests/unit/control/test_cloud_engine.py::test_cloud_engine_delegates_assets_mapping_to_ingestor tests/unit/control/test_engine_write_middle_path.py::test_write_delegates_assets_mapping_to_ingestor` 全绿（exit 0） |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档归档**接入层各实现的实现规约**：每个 `*_impl/` 实现对应哪个接口契约、注册方式（或不注册）、依赖、产出 与各自取舍。接口契约本身（方法签名、错误语义、Write 路径不变量）归 `docs/specs/S01-ingest-access.md`；本文聚焦「当前有哪几种实现、各自怎么落地」。

---

## 背景

接入层（架构 §10/§10.1，Write 路径前半段）承接多模态信息源，对每条原始数据做两件事（让检索链路不感知模态）：**保留原模态资产引用** + **规约出可治理文本/结构投影（content）**，再转成 `MemoryUnit`。**本层不落盘**——返回的记忆单元交构建层写真源建索引。

算子拆成两个互补角色：

| 角色 | 接口 | 职责 | 是否走工厂 |
|---|---|---|---|
| **Source**（信息源连接器） | `ingest/source.py` | 只「取到原始数据」→ 统一为 `RawPayload`；不规约、不编排 | **否**（无 Producer，手动构造） |
| **Ingestor**（接入编排） | `ingest/ingestor.py` · `IngestorProducer`（TOP_NAME=`ingestor`） | 规约（调 Normalizer）+ 转换为 `MemoryUnit`（不落盘） | **是**（`@IngestorProducer.register`） |

> 「规约投影」= 把五花八门的来源（对话/PDF/代码/图片/录音/视频）统一翻译成一份系统能处理的文字描述（content），原件留在 `assets`、影子存进 content。换更好的转写模型后拿原件重跑 Normalizer 即可重建投影。具体翻译落在 `common.Normalizer`，接入层只编排、不实现规约算法。

### 注册铁律

- **Ingestor** 走「两级命名空间 + Producer」装配：实现文件尾部 `@IngestorProducer.register("simple")` 自注册，`ingestor_impl/__init__.py` import 触发，`ingest.bootstrap.register_ingestors()` 在装配前统一调用（幂等）。Normalizer 依赖经 `NormalizerProducer.dep(config, default="passthrough")` 自取（与该 Producer 生成/共享同一实例）。
- **Source** **不注册到任何工厂**：`TextSource` 由上层（信息源接入代码/测试）按需直接 `TextSource(scope, items)` 构造——它的入参是「这一类源的连接配置」（如预置条目、连接串），不属于内核装配的依赖图。`source_impl/__init__.py` 仅重导出实现类，无 Producer。

---

## 决策：各实现规约

### Ingestor（`ingest/ingestor.py` · `IngestorProducer` · TOP_NAME=`ingestor`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `simple` | `SimpleIngestor` | `normalizer`（`dep`，缺省 `passthrough`） | `list[MemoryUnit]` | 对每条 `RawPayload`：先校验 modality 在 `Normalizer.modalities()` 中，再调 `normalize` 规约出 content，包成单 `Segment(content, assets=list(payload.assets), source=modality)` 的 `MemoryUnit`；分配 `uuid4` id、`scope` 透传、`source_ref=payload.id`、双 metadata 拷贝 |

**双时间写入**（`Temporal`）：

- `t_event` = `payload.occurred_at or now`（事件发生时刻，缺省取接入当下）
- `t_ingest` = `now`（接入时刻）
- `t_valid` = `now`（自接入起有效）；`t_invalid` 不设（未失效）

**不做的事**（边界）：`tags` 不在此设置，仍由控制层处理；**不落盘**（归构建/控制层）；不分块、不向量化（归构建层）。资产映射是 Ingestor 责任，但“一个 payload 产出单 MemoryUnit/单 Segment、全部 assets 放该 Segment”只是当前 `simple` 实现策略，不是接口固定数量契约。

### Source（`ingest/source.py` · 无 Producer，手动构造）

| 类 | 模态 | 构造入参 | `fetch` 语义 |
|---|---|---|---|
| `TextSource` | `TEXT` | `scope`、`items: Sequence[(text, occurred_at)]` | 把预置 `(text, occurred_at)` 列表拉成统一 `RawPayload`（TEXT 模态，`data=text.encode("utf-8")`，分配 `uuid4` id，`occurred_at` 标在字段上）；`since` 非空时只返回该时刻**之后**的条目（增量拉取，`occurred_at <= since` 跳过） |

> `TextSource` 用一批预置文本模拟一类信息源（对话/导入），是离线/测试用的最小连接器。连接器只取原始数据，规约归 Normalizer、编排归 Ingestor。

---

## 拒绝的方案

- **Source 也走 Producer 注册**：被拒。Source 的构造入参是「某一类源的连接配置」（预置条目、外部连接串、增量游标），属于信息源接入侧而非内核装配依赖图；强行塞进两级命名空间会把「连接哪个源」和「装配哪个算法」混为一谈。故 Source 保持纯接口 + 手动构造，只有 Ingestor（依赖 Normalizer、是 Write 路径内核算子）走工厂。
- **Ingestor 内联实现规约逻辑**：被拒。各模态翻译（OCR/ASR/正文解析）归 `common.Normalizer` 插件，Ingestor 只编排（调 normalize + 转 MemoryUnit），保证「换转写模型 = 换 Normalizer，接入编排不动」。
- **接入层落盘**：被拒。Write 路径职责分层——接入层只产出 `MemoryUnit`，写真源 + 建索引归构建层；落盘留在一处便于事务/版本控制。
- **`t_event` 缺省留空**：被拒。`occurred_at` 缺失时回退 `now`，保证时序字段恒有值，下游 as-of 查询/生命周期判定无需特判空。

---

## 验证

- `tests/unit/ingest/test_simple_ingestor.py` 直接覆盖 assets 映射及防御性复制。
- 同文件覆盖不支持模态在 normalize 前失败，以及自定义 Normalizer 声明支持后正常调用。
- InMemoryEngine/CloudEngine 的定向用例用自定义多 Segment Ingestor 验证 Engine 只透传 assets、不改写映射结果。
- `TextSource` 为离线/测试连接器，随 write 路径与各源接入用例运行。

---

## 已知遗留

- **仅 `simple` 一种 Ingestor**：当前接入编排只有单 Segment 最小实现；更复杂的多 MemoryUnit/多 Segment 映射策略留给后续 Ingestor 实现。
- **仅 `TextSource` 一种连接器**：真实信息源（对话流/文档/代码库/工具轨迹/图像/音视频/外部导入）的连接器尚未落地，目前以预置文本模拟。
- **资产原件存储不在本次范围**：当前贯通的是 `list[str]` 引用传递与映射；引用对应的原件是否由 FSStore 管理，仍由具体接入/部署方案决定。
