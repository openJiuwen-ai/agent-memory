# F08 — 文档记忆（markdown 视图 + 影子索引 + 看门狗）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-09 |
| 对应 commit | `f84097a` feat(memory): 文档记忆初版实现（markdown 真源 + 影子索引 + 看门狗） |
| 影响范围 | `jiuwen_memory/storage/`（markdown / shadow / watchdog / sync_gate / domain_store / store_manager）、`jiuwen_memory/config/document_flag.py`、`jiuwen_memory/construction/`（document_index_builder / llm_extractor / llm_router / 两个 evolver）、`jiuwen_memory/retrieval/`（pipeline_retriever / shadow_recaller）、`jiuwen_memory/api/memory_api_impl/`（assembly / write_ops / local_support）、`jiuwen_memory_entry/`（server / http_server / mcp_server）、`pyproject.toml`（deploy extras） |
| 测试基线 | `tests/unit/storage/`（test_local_markdown_store / test_sqlite_shadow_index / test_local_watchdog / test_sync_gate / test_composite_storage / test_storage_base）、`tests/unit/config/test_document_flag.py`、`tests/unit/construction/`（test_document_index_builder / test_extractor_title / test_llm_router）、`tests/unit/retrieval/`（test_shadow_recaller / test_pipeline_retriever_doc_mode）、`tests/unit/api/test_memory_runtime_lifecycle.py`、`tests/unit/api/test_collective_routing.py`、`tests/unit/config/test_storage_routing.py` |
| Refs | 仓根设计文档 `F07-document-memory-redesign.md`、`F08-document-memory-impl.md`（本文以已落地代码为准） |

## 背景

KV 时代的记忆真源是 `KVStore` 里的 `memory_codec.dumps(unit)` 字节——机器可读、人类不可读。要查看一个空间里到底记了什么，只能走 `list` 接口反序列化，或者直接打开 sqlite 看二进制。同时倒排（fulltext）、向量（vector）各自独立成 Store，写入侧由 `HybridIndexBuilder` 分别投影，装配链路长、后端依赖重（Milvus / Elasticsearch）。

文档记忆改变真源形态：

1. **markdown 文件是人类可读视图**——记忆按归属类别 + 项目坐标落成 `USER.md` / `MEMORY.md` / `daily_memory/日期.md`，用编辑器直接打开即读；
2. **影子索引是机器真源**——单个 sqlite 文件（FTS5 倒排 + sqlite-vec 向量 + 全量 `unit_json` 三表同库），承接点查、检索与 `list`；
3. **看门狗双向同步**——用户手改 md 后，把改动以 unit 粒度增量同步回影子索引，保持两侧一致。

md 与影子索引的关系是**单向分工**而非互为镜像：召回走影子索引按 `unit_id` 取全量，**不靠 md 反解**；md 只承载正文 + 标题，元数据不进 md。

## 目标

1. `globals.write_document=true` 时，写入真源从 KV 整体切换为 md + 影子索引，调用方 API 契约不变。
2. md 落盘路径按 `memory_class`（归属类别）+ `project`（coords 坐标）分流，人类可按目录导航。
3. 影子索引一个算子承载全量存储、点查、倒排、向量四种能力，缺 embedder / sqlite-vec 时优雅降级。
4. 用户手改 md 能被监听并同步进影子索引（unit 粒度增量，不整文件重建）。
5. 写入路径与看门狗对账之间无回环竞态（写窗口防护）。
6. 非文档模式（默认）行为完全不变，两套真源互不污染。

## 非目标

- 不做 md → 记忆的反解真源（召回不解析 md）。
- 不做跨进程文件锁（首版 `threading.Lock` 进程内串行化，本地单进程场景）。
- 不做看门狗 pause/resume（算法不幂等，列为待办，靠 debounce 缓冲）。
- 不在本版做文档模式的 graph / entity 检索扩展（graph 路按端口就绪并存，entity 未接）。
- 不改 KV 路径的任何行为。

---

## 决策

### 一、开关与装配期归一：`config/document_flag.py`

| 开关 | key | 未配置默认 | 语义 |
|---|---|---|---|
| 文档写入 | `globals.write_document` | `False` | true = 真源写影子索引 + md，不写 KV |
| 文档看门狗 | `globals.watch_document` | `True`（随文档） | 仅 `write_document=true` 下有意义 |

两个开关的归一函数（`should_write_document` / `resolve_watch_document`）共用字符串/数值归一逻辑，**唯独 None 语义不同**：写入开关未配 = 关（默认不写文档），看门狗未配 = 开（开了文档就该监听）。不识别的值（如 `write_document: yes` 拼写错误）抛 `ValidationError` **fail-closed**，不静默回退——避免拼写错误整体吞掉文档路径。

归一结果在各消费方 `_build` 装配期**固化进实例属性**（如 `CompositeDomainStore._write_document`、`PipelineRetriever._doc_mode`），运行期方法直接读属性、不再持 config 句柄——与 `_preferred_pipeline` 等现有开关同范式。

`resolve_index_builder_default(config)` 是 IndexBuilder 缺省实现名的判定中枢：文档模式 → `document`；非文档随 `vector_enabled` → `hybrid` / `fulltext`。**三处消费方必须共用**（`in_memory_engine._build`、`orchestrating_evolver._build`、`dynamic_evolver._build`），否则缺省判定分叉会让同一份装配拿到不一致的 IndexBuilder——文档模式错配 hybrid 即真源写错地方。

### 二、md 视图存储：`MarkdownStore`（`storage/markdown.py` + `markdown_impl/local_markdown_store.py`）

落盘路径按 `memory_class` + `coords.project` 映射（空值兜底 `team_memory` / `default`）：

| memory_class | md 路径 | 说明 |
|---|---|---|
| `user_memory` | `{root}/memory/USER.md` | 跨 project，用户画像 |
| `project_memory` | `{root}/memory/{project}/MEMORY.md` | 单文件块追加 |
| `team_memory`（含兜底） | `{root}/memory/{project}/daily_memory/YYYY-MM-DD.md` | 按天聚合 |

块格式恒为 `# {标题}\n{正文单行}\n\n`：

- **标题分流**：daily 文件用 `coords["team"]`（标识来源团队，缺失兜底 `unit.id`）；其余文件用 `system_metadata["md_title"]`（LLM 抽取时与 tier/tags 同 prompt 生成），缺失兜底 `unit.id`。标题行在任何机器读回路径中不被解析，内容变化对机器路径零影响。
- **正文单行**是存储层强约束（见决策六）。

`write` 落盘后**就地回填** `unit.system_metadata[MD_FILENAME_KEY]`（md 相对路径），供影子索引落 `memory_unit.md_filename` 列、看门狗定位文件。整批共持一次锁、按文件分组一次追加写（同文件多 unit 的块拼一起，减少 IO）。

`replace_content` / `remove_content` 服务 update / delete 的 md 侧同步：按 `\n\n` 切块、标题行后正文 == 目标 content 命中，整块替换 / 删除**首个**命中；未命中返回 False（md 与索引漂移，交看门狗对账，不抛错）。

**路径安全收口**（`_safe_abs_path`）：`md_filename` 源于调用方可写的 `system_metadata`，不可信——绝对路径直接拒绝；`realpath` 归一双方后 `commonpath` 前缀校验（消解 `..`、双斜杠、symlink；Windows 跨盘符 ValueError 视为逃逸）。逃逸抛 `ValidationError` fail-closed。已知边界：realpath 与 open 之间存在 TOCTOU 窗口，本收口面向「恶意字符串」威胁模型，非对抗本地竞争者的沙箱。

`root` 支持 ConfigSource 晚绑定（`markdown_store.root`），缺失回退构造期默认值。

### 三、影子索引：`DocumentShadowIndex`（`storage/shadow.py` + `shadow_impl/sqlite_shadow_index.py`）

**复合算子**——与 KVStore/FulltextStore/VectorStore 单一契约不同，写入入口是全量 `MemoryUnit`，`content` 正文、`embedding` 向量在算子内部派生；唯 `md_filename` 例外（由 `md.write` 回填进 system_metadata 后传入）。

一个 sqlite 文件三张表，同库、同连接、同事务，靠 `unit_id` / 隐式 `rowid` 关联：

| 表 | 角色 |
|---|---|
| `memory_unit` | 全量真源（`unit_id` 主键 + `unit_json` BLOB + content_hash/md_filename/project/category/lifecycle/t_valid/t_invalid/t_event 投影列） |
| `memory_fts` | FTS5 倒排（普通 FTS5 表自存 token 串，非 external content） |
| `memory_vec` | sqlite-vec `vec0` 向量表（仅完整模式建） |

**降级机制**：`embedder` 与 `sqlite_vec`（deploy extras 软依赖）均可选。完整模式（embedder 注入 + sqlite_vec 可加载）三表全建；降级模式只建前两表，`search_vector` 返回空列表不抛错——上层检索编排照常运行，只是向量召回缺一路。`vec_enabled` 依赖运行期 `sqlite_vec.load` 结果，连接建立前返回 False，消费方应在已建连接的路径读取而非装配期探测。

**db_path 派生优先级**：`shadow_index.db_path` 显式配置 > `{markdown_store.root}/.shadow/shadow.db` 派生 > 构造期 fallback。第 2 级让 db 自动跟随 markdown 根目录——用户只配一处 root，影子索引物理伴生在 md 根下。

**CRUD 语义**（对齐 KVStore 惯例）：

- `insert_units`：unit_id 已存在抛 `ConflictError`；同事务写全量 + FTS5 + vec0。
- `update_units`：id 不存在抛 `NotFoundError`；按 `content_hash` 变化判定投影重建——content 改（OVERWRITE）→ 重建倒排 + 向量；只改状态字段（SUPERSEDE）→ 只覆写 `unit_json`。投影列（lifecycle 等）无论 hash 是否变都覆写（谓词下推依赖）。重建走 DELETE + INSERT 而非原地 UPDATE（vec0 无 UPDATE 语义，FTS5 原地行为依赖实现版本）。**投影列空兜底守卫**：coords 是 TRANSIENT 键（dumps 剥除），读回的 unit 永远没有 coords → `_project_of` 落 default；若不守卫，任何 read-modify-write 循环（SUPERSEDE / dedup / LifecycleManager）都会把原 project 重置为 default，下次按 project 隔离召回即丢失。project / md_filename / category 取到兜底值时保留旧列值。
- `delete_units`：幂等（缺失静默跳过），同事务显式删三表（rowid 关联无级联）。
- `get_units`：缺失 id 省略不抛错（服务召回物化，命中 id 中途被删应跳过而非整批失败），按输入顺序保序返回。
- `list_units`：全量拉 `(unit_id, unit_json bytes)`，过滤/排序/分页交上层复用 `list_memory_entries`（与 KV `scan→list` 同构）。

**召回（`search_fulltext` / `search_vector`）**，project + category 过滤在算子内部分两批下推：

- 批 1（受 project 隔离）：`project IN (...) AND category IN ('project_memory','team_memory')`
- 批 2（全局可见）：`category = 'user_memory'`（无 project 限制）

project 过滤值取自 `query.filters` 里的 `system_metadata.project` 谓词（上层 coords 折算下推，`_projects_from_filters` 深度遍历收集、滤空串）；无该谓词落 `default`——失效方向是放宽，与坐标缺项语义一致。**隔离不走 Scope 字段**：影子索引的 `scope` 入参仅作签名占位对齐契约，写入不落 scope 列。

系统前置谓词（lifecycle/t_valid/t_invalid/t_event）经 `_compile_system_filters` 编译成 SQL WHERE 索引级下推（对齐非文档流程 `build_system_filters`）；OR 组含无约束 child 整体放弃下推、NOT 不产生（点读后 `is_retrieval_candidate` 复核兜底）。

FTS5 细节：tokenize 用 `unicode61`——写入前已用注入 tokenizer（jieba）预分词成空格分隔 token 串，unicode61 按空格切分即还原 token；查询侧同样先分词再 MATCH，且 token 间用 **OR 连接**（FTS5 空格是隐式 AND，自然语言查询带疑问词/停用词会让整条 0 命中）。向量召回是 post-filter：批 1 用 `k * 4` 过采样兜底召回不足。

### 四、写入路径分流：`CompositeDomainStore` 文档分支

`should_write_document()` 为 true 时，领域方法整体切到文档路径（互斥分支，非叠加）：

| 方法 | 文档路径 |
|---|---|
| `add` | `_sanitize_document_content` → `md.write`（回填 md_filename）→ `shadow.insert_units`；不碰 KV |
| `update` | 逐条：`shadow.get_units` 取旧 content → `shadow.update_units` 覆写 → content 变时 `md.replace_content`；SUPERSEDE 只改状态字段 content 不变 → md 不动 |
| `delete` | `shadow.get_units` 取旧 unit（md_filename + content 定位 md 块）→ `shadow.delete_units` 删三表 → 逐条 `md.remove_content` |
| `get` | `shadow.get_units`（缺失省略、保序），替代 `kv.mget` |
| `list` | `shadow.list_units` 全量拉 → 复用 `list_memory_entries` 内存过滤排序分页 |
| `SOFT` remove | no-op——检索退出由调用方先 `lifecycle.transition` 改状态（`update(FORWARD_ONLY)` 同步 lifecycle 投影列），检索侧靠谓词下推 + retriever 复核排除；**调用方契约：先 transition 再 remove(SOFT)** |

**单行清洗**（`_sanitize_document_content`）：块格式契约（单行正文、看门狗按行遍历、replace/remove 按行比对）建立在「一个 unit 一行正文」上，但上游不保证——content 含换行时 md 块被切碎、看门狗把第 2+ 行当独立幽灵 unit。故在 md.write / shadow.insert_units 分叉**之前**对 unit 本体原地折叠空白为单空格——md 视图、unit_json、content_hash、replace_content 锚点四方看到同一份 content。收口在文档路径入口而非 extractor：单行是**存储层约束**，KV 路径不受影响。

**写窗口防护**（`storage/sync_gate.py`，F07 §12.9 风险 6）：文档路径是两步写（md + 索引），两步之间 md 与索引短暂不一致，看门狗 check-then-act 对账会误判漂移（add 窗口 → 幽灵 unit 双写；update/delete 反序窗口 → 删真 unit / 复活已删 unit）。防护：写入编排层在两步写之前 `open_write_window()`、`finally` 里 `close_write_window()`（精确覆盖含慢 embed 的全过程——完整模式逐条 embed 是远端 HTTP，窗口可达秒级，2s debounce 挡不住）；看门狗 sync 入口查 `write_window_open()`——窗口开着**推迟**本轮对账（0.25s 轮询，窗口一关立即续跑），超过 60s 上限放弃本次（防写路径 bug 永不关窗卡死任务；放弃不丢数据，下一次 md 文件事件会重新触发）。门闸是**深度计数**非布尔位（支持嵌套写路径），模块级单例——composite 与 watchdog 无需互相持有引用，同进程 import 即共享。

### 五、召回路径：`ShadowRecaller` + `PipelineRetriever` 文档模式

文档模式下召回统一走 `ShadowRecaller`（`storage/domain_store_impl/shadow_recaller.py`），替代 KV 时代的 keyword + vector + layers 多路（那些路取 fulltext/vector 端口，文档模式不装配 → 恒返空）。graph 路独立于 fulltext/vector 端口，按 `graph_enabled` 且 `has_graph()` 决定是否并存。

- **通道**：`RecallChannel.DOCUMENT`——关键词 + 向量合一的单通道（影子索引本就是复合算子）。`ParsedQuery` 里有 `vector` 且 `vec_enabled` 时走 ANN 补充路，否则单走 FTS5。
- **通道补全**：parser 产出的 `parsed.channels` 默认 `[KEYWORD, GRAPH]`（+可选 VECTOR）不含 DOCUMENT，`storage._recall` 按 `r.channel() in channels` 过滤 recaller——漏补即文档模式召回**恒空且不报错**。`PipelineRetriever` 构造签名新增 `doc_mode`，`retrieve()` 据此把 DOCUMENT 补进 enabled（仅补不替，调用方显式传 `query.channels` 仍尊重其选择）。
- **RRF 合并**：两路结果按名次做 Reciprocal Rank Fusion（k=60）。**不按分数 max 归并**：两路分数口径相反且量纲不可比（bm25 负值越小越相关；向量返回负距离越大越相关）——max 归并会让 fulltext 恒压制 vector 或排序整体颠倒。RRF 只消费名次（两路返回时已按相关度排好序），方向与量纲问题一次性消除；同一 unit 两路都命中时贡献累加。
- **物化**：`ScoredID.metadata` 不透传 evidence，物化侧从 `shadow.get_units` 重取完整 MemoryUnit。
- **生产过滤**不在召回算子做——三重排除已覆盖：① 影子索引把系统谓词编译进 SQL（索引级排除）；② retriever 三条 pipeline 物化后统一过 `is_retrieval_candidate` 复核；③ 调用方「先 transition 再 remove(SOFT)」契约保证投影列已同步。
- 文档模式下 `PipelineRetriever._build` 不再构造 `UnitReader`（传 None）——KV 不再持有 MemoryUnit，点读一律走 domain store 的 `shadow.get_units`。

### 六、写入链路的坐标透传（construction + api 侧）

文档分流依赖 `coords.project`（md 路径）与 `memory_class`（md 路径 + 影子索引 category 列），两个断点在本次 commit 修复：

1. **判定产物回写 coords**（`write_ops._routes_by_decision` / `OrchestratingEvolver._route`）：coords 在 API 入口被 `_take_coords` 取出，判定产物只回写标签与类别——不塞回则 md 落盘与影子索引都读不到坐标，project_memory 全落 `memory/default/`。两处从 `ctx.coords` / `coords` 回写进 `merged[COORDS_KEY]`。coords 是 TRANSIENT 键，dumps 进 unit_json 时剥除，但 md.write 与 shadow 在序列化**之前**从 unit 对象读，路径计算不受影响。
2. **回显剥瞬态键**（`local_support._strip_transient_metadata`）：write 返回的 unit 是同一批对象引用，原样回显等于 coords 越过 API 边界——与 `ROUTE_CTX_KEY` 同一处置：内部消费完，出口剥净。无瞬态键时不复制原对象直返（多数写入不走判定，不为不变式买单）。

配套改动：

- **LLM 抽取生成标题**（`llm_extractor`）：`ExtractionCandidate` 新增 `title` 字段，与 tier/tags 同 prompt 产出（命名「这件事」非类别，~20 字符）；`_parse_title` 清洗保单行 + 截断 ≤50 字符（防模型违约破坏块格式）；空串 = 无标题，落盘侧兜底 `# {unit.id}`。procedural 路径同 prompt 一并产出。
- **LLM Router 注入坐标**：system prompt 增加 `OWNERSHIP COORDINATES: {coords}` 段——判定 memory_class 归属时参考具体实体（关于 project 的事实是 project_memory，即使出自用户之口）。
- **memory_class 复用**：`MEMORY_CLASS_KEY` 本是归属判定的类别名，文档记忆复用它作 md 分流依据（不另设新键）。它是落盘键（不在 TRANSIENT 集合），dumps 保留、读回可查。

### 七、看门狗：`LocalWatchdog`（`storage/watchdog.py` + `watchdog_impl/local_watchdog.py`）

**与 Store 算子的差异**：Store 是被动数据后端；看门狗是**反向驱动组件**（主动监听文件事件 → 反向调影子索引 insert/delete）。故不继承 `BaseStore`、不进 `StorageCapability` 枚举、不被 `_stores` 持有——生命周期独立挂在 `Kernel`，随事件循环 start/stop。

**事件桥接**（F07 §12.8 方案 B 变体）：`watchdog` 库 Observer 线程的 `on_modified/on_created/on_deleted` 回调经 `loop.call_soon_threadsafe` 投递到主事件循环，`asyncio.create_task` 起异步同步任务，sqlite 操作经 `asyncio.to_thread` 推到独立线程避免阻塞事件循环。不复用 `Job`/`Scheduler`（事件驱动非周期触发）。

**关键机制**：

- **unit 粒度增量 diff**（非 lite 的文件级全量重建）：`shadow.list_units_by_md` 拿旧 `(unit_id, content_hash)` → 读 md 按行算新 hash → diff 出新增/删除集合只动变化的 unit。整文件重建会丢其他 unit 的 id（破坏 supersedes 链）。
- **按行遍历**：`#` 开头标题行跳过、空行跳过、正文行算 sha256——前提即决策四的单行约束。
- **debounce 按文件分键**：`_watch_timers` 按 md_filename 各持一个 timer——单一 timer 时 B 文件的事件会 cancel 掉 A 文件待执行的同步，而 A 无新事件不会再补（纯事件驱动），改动永久丢失。
- **初始宽限期**：启动后延迟 1s 置 `watcher_initialized`，避开 Observer 刚起时对存量文件的初始扫描风暴（存量 md 是写入流程产物，不是「用户手改」）。
- **写窗口推迟**：见决策四。`_do_sync` 里轮询 `write_window_open()`。
- **unit_id 策略**：用户改某行 content → 旧 hash 消失 + 新 hash 出现 → 删旧 unit + 建新 unit（**新 uuid**），不保留旧 id（F07 §12.9 风险 5 当前版本策略——改行即断版本链，见已知遗留）。新建 unit 的缺省元数据：tier=SEMANTIC、t_ingest=now、provenance=`["watchdog_sync"]`、project 与 memory_class 从 md 路径反推（`daily_memory/` → team_memory、`MEMORY.md` → project_memory、`USER.md` → user_memory）。
- **监听目录**：`{root}/memory/` 递归；目录尚不存在时退监听 markdown_root 本身（不能 schedule 不存在的目录）。

装配（`WatchdogProducer.register("watchdog")`）：shadow 与 markdown 一律从注入的 StoreManager 取端口（不自行 build_named，保证与 CompositeStorage 读写同源）；markdown root 从 `MarkdownStore.root` 契约读（ConfigSource 晚绑定解析后的值，不跨 namespace 摸 params 字面量）；scope 用空 `Scope()` 占位（影子索引不按 scope 隔离）。

### 八、端口与生命周期

**端口**：`StoreManager` ABC 新增 `markdown()` / `shadow_index()` 及 `has_*`（带缺省实现——未装配时 has 返 False、取端口抛 `UnsupportedStorageCapabilityError`）；`StorageCapability` / `StoreType` 枚举新增 `MARKDOWN` / `DOCUMENT_SHADOW`。`CompositeStoreManager` 按 `markdown_store` / `shadow_index` 命名空间扫描聚合端口（与七类既有 Store 同范式，配置不声明即不装配）；`RoutingStoreManager` 补齐对应惰性代理。**是否装配端口与 `write_document` 开关独立**——端口由命名空间声明决定，开关由数据面实例属性判定，两者错配时 `DocumentIndexBuilder` / `ShadowRecaller` 构造期 fail-closed 抛错。

**生命周期**（F07 §12.10，`api/memory_api_impl/assembly.py`）：`build_kernel` 是同步函数、装配期无 running loop，看门狗的 `start`（内部 `asyncio.get_running_loop` 取 loop 绑 Observer 桥接）必须延后。`assemble_runtime()` 返回 `MemoryRuntime`（`api` + 生命周期，不暴露 kv/storage 端口）：

- **异步面** `await start_background()`：FastAPI lifespan / MCP / SDK provider 调，看门狗绑当前 loop。
- **同步面** `start()`：http_server 等无事件循环的宿主编译，daemon 线程自持专属 loop（`call_soon(watchdog.start)` 后 `run_forever`）。
- `close()`：先停看门狗（含「已装配但从未 start」的告警日志），再关影子索引 sqlite 连接，再停专属 loop，最后关任务池。幂等。

宿主接入：`Server` 暴露 `start_background` / `start`；`HttpServer.serve()` 调 `self._runtime.start()`；MCP server 用 FastMCP `lifespan` 调 `start_background`。**不调则文档看门狗不启动、md 手改监听静默失效**。

**依赖**：`watchdog>=4.0` 与 `sqlite-vec>=0.1` 进 pyproject `deploy` extras；sqlite-vec 代码侧 try/except ImportError 软依赖（缺装即降级两表模式）。

### 九、文档模式的 IndexBuilder：`DocumentIndexBuilder`

文档模式下 `resolve_index_builder_default` 把三处缺省指向 `document` 实现——「全委托 DomainStore」的薄编排层：`build/update/remove` 把 units 与 mode 原样下传给 `domain_store.add/update/delete`，由 `CompositeDomainStore` 内部按 `should_write_document` 分流到 md + 影子索引。

为什么是 IndexBuilder 的一种实现而非另开 engine 路径：`InMemoryEngine.write` 默认路径只调 `index_builder.build`，契约要求「记忆写入只经本算子」——文档模式必须经由 IndexBuilder 接住调用。把真源落盘收进 `domain_store.add` 的文档分流、再由本算子委托，既不破 engine 契约，也不让 `md_filename` 回填时序泄漏到 construction 层（md.write 与 shadow.insert_units 闭环在 domain_store.add 同一调用栈内）。

构造即校验：注入的 DomainStore 非文档模式直接抛 `UnsupportedStorageCapabilityError`，不拖到首次写入才以 AttributeError 暴露。

## 拒绝的方案

- **md 作为可反解真源（FTS 反解 / 从 md 重建 unit）**：被拒。md 只承载正文 + 标题，元数据不进 md，反解必然丢信息；召回走影子索引按 unit_id 取全量 `unit_json`。md 是人类视图，机器路径不依赖解析它。
- **看门狗做成 Store（继承 BaseStore、进 StorageCapability、被 manager 持有）**：被拒。它没有 CRUD 动词、不提供存储能力、生命周期随事件循环而非构造——硬塞会污染 BaseStore 语义与 capabilities() 语义。
- **看门狗复用 Job/Scheduler**：被拒。它是事件驱动非周期触发，周期任务框架不适配；异步任务用 `create_task` + `to_thread` 桥接。
- **FTS5 external content 模式**（`content='memory_unit'` 不重复存正文）：被拒。external content 要求关联表有同名 `content` 列而 memory_unit 只有 `unit_json`，任何解析 content 列的查询（含 `count(*)`/MATCH）报 `no such column: T.content`。改普通 FTS5 表自存 token 串，代价是 token 串存两份但体积可控。
- **FTS5 `tokenize='simple'`**：被拒（运行环境 SQLite 不含该 tokenizer，仅 unicode61/ascii/porter）。改 unicode61 + 注入 tokenizer 预分词成空格分隔 token 串——「FTS5 只管倒排结构、分词归预处理器」的设计意图不变。
- **FTS5 查询 token 间隐式 AND**：被拒。自然语言查询常带疑问词/停用词，任一词不在文档里整条 0 命中，召回普遍落空。改 OR 连接，弱相关由 top_k 截断与上层 RRF 消化。
- **fulltext 与 vector 召回按分数 max 归并**：被拒。两路分数口径相反（bm25 负值越小越相关 vs 负距离越大越相关）且量纲不可比（无界对数 vs 欧氏距离），任何换算后取 max 都会让一路恒压制另一路。RRF 只消费名次，方向与量纲问题一次性消除。
- **FTS5/vec0 原地 UPDATE 重建投影**：被拒。vec0 无 UPDATE 语义、FTS5 原地 UPDATE 行为依赖实现版本。显式 DELETE + INSERT 语义清晰、跨版本一致。
- **写窗口用布尔位**：被拒。嵌套写路径（add 内含 update 等）会在内层 close 时提前放行，让看门狗插进外层的两步写窗口。深度计数门闸支持嵌套配对。
- **窗口开着时看门狗丢弃本轮事件**：被拒。窗口内用户真实手改 md 的漂移会被吞掉。推迟（轮询等窗口关闭后重扫）不丢数据；超时上限 60s 防写路径 bug 永不关窗卡死任务。
- **看门狗 debounce 单一 timer**：被拒。多文件并发改动时 B 文件的 cancel 会吞掉 A 文件待执行的同步，A 无新事件不会再补，改动永久丢失。按 md_filename 分键。
- **单行清洗收口在 extractor**：被拒。单行是文档记忆的存储层约束（块格式 + 看门狗遍历口径），非抽取层约束；直写路径（infer=false）与看门狗重建路径不过 extractor。收口在文档路径入口（`_sanitize_document_content`），KV 路径不受影响。

## 验证

- `tests/unit/config/test_document_flag.py`：开关归一的 fail-closed 边界（拼写错误抛 ValidationError）、None 语义差异（write 未配 False / watch 未配 True）、`resolve_index_builder_default` 三分支。
- `tests/unit/storage/test_local_markdown_store.py`：路径映射（三类 memory_class + 兜底）、块渲染、md_filename 回填、replace/remove 定位与未命中、路径逃逸拒绝。
- `tests/unit/storage/test_sqlite_shadow_index.py`：三表写入、CRUD 语义（冲突/缺失/幂等）、content_hash 判定投影重建、空兜底守卫、两批过滤召回、系统谓词下推、降级模式。
- `tests/unit/storage/test_local_watchdog.py`：事件桥接、debounce 分键、unit 粒度 diff、路径反推、幂等 stop。
- `tests/unit/storage/test_sync_gate.py`：嵌套配对、close 不把深度降到负、单例初始关闭。
- `tests/unit/storage/test_composite_storage.py`：文档分流（add/update/delete/get/list 五路）、写窗口包裹、单行清洗。
- `tests/unit/construction/test_document_index_builder.py`：薄委托、非文档模式构造期抛错、mode 透传。
- `tests/unit/construction/test_extractor_title.py`：title 解析清洗（单行 + 截断 + 容错）。
- `tests/unit/retrieval/test_shadow_recaller.py`：fulltext 主路 + vector 补充路 + RRF 合并 + 降级模式返空。
- `tests/unit/retrieval/test_pipeline_retriever_doc_mode.py`：DOCUMENT 通道补全（漏补即召回恒空且不报错）。
- `tests/unit/api/test_memory_runtime_lifecycle.py`：start 两种语义、close 组合释放顺序。
- `tests/unit/config/test_storage_routing.py` / `tests/unit/api/test_collective_routing.py`：RoutingStoreManager 新端口、coords 判定回写。

## 已知遗留

- **看门狗改行断版本链**：用户改某行 content → 删旧 unit + 建新 uuid unit，supersedes 链断裂（F07 §12.9 风险 5 当前版本策略）。后续可探索按标题行锚定保留旧 id。
- **replace_content / remove_content 首个命中**：content 重复时只改/删第一处，可能误替多块（§5.2.3 注）；后续可加 unit_id 锚点优化。
- **跨进程文件锁未做**：md 与 sqlite 都是进程内 `threading.Lock` 串行化，多进程并发写同一 root 无防护（与 sync_gate 跨进程不防护同口径）。
- **TOCTOU 窗口**：`_safe_abs_path` realpath 校验与 open 之间存在竞争窗口，收口面向恶意字符串威胁模型。
- **SOFT remove 是 no-op**：依赖调用方「先 transition 再 remove(SOFT)」契约，裸调 SOFT 不会使 unit 退出检索。
- **`ShadowRecaller` 模块 docstring 自称「接口骨架」**：与实现现状不符——fulltext/vector/RRF 均已落地，注释滞后待清理。
- **graph / entity 检索未接文档模式**：graph 路按端口就绪并存；entity 扩展未接（构造器持有的 `_storage` 预留点读真源能力）。
- **`list_units` 全量拉取**：大表场景全量载入内存过滤分页，与 KV scan 同构但无分页下推；数据量大后需考虑 SQL 级分页。

## 后续演进

- **看门狗 pause/resume**（watchdog-watch-document-switch 待办）：算法不幂等，暂停窗口内的变更靠下一轮事件补扫，需设计重扫描机制。
- **`MemoryRuntime` 协议面推广**（F07 §12.10）：SDK 面 `assemble_runtime` + `start_background` 已落地，venv 侧旧版 provider（0.1.17 identity=/metadata= 旧契约）与新 API 不兼容，端到端装配缺口见 doc-memory-e2e-assembly-gap 记录。
- **文档记忆对外 add 端点**：SDK 侧文档记忆覆盖与凭证注入缺口（e2e-doc-memory-sdk-config-gaps 记录）。
