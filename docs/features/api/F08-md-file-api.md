# F08 — md 文件级接口（read_md_file / edit_md_file）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-15（D4 修订：删旧建新 → 继承式 SUPERSEDE） |
| 影响范围 | `jiuwen_memory/api/`（`memory_api.py` + 新增 `memory_api_impl/md_ops.py`）、`jiuwen_memory/control/`（`engine.py` + `engine_impl/*` + `application/query.py` / `command.py`）、`jiuwen_memory/storage/`（`markdown.py` / `shadow.py` / `domain_store.py` + 各 `*_impl`）、`docs/specs/S02-memory-api.md` |
| 测试基线 | 待实现（见「验证」节） |
| Refs | 待定（issue 号未确认，不臆造） |

> 本文是 `jiuwen-memory-md文件接口设计.md`（仓库根，方式 A / B 分析）在代码层面的落地决策。那份文档论证了「方式 A（内核新增接口）可行、方式 B 不可行」，本文不再重复论证，只记录方式 A 的**落地决策**与**被拒绝的实现路径**。

## 背景

jiuwen-memory 对接 jiuwenswarm 时只暴露 `add` + `search`，而 jiuwenswarm normal 模式原有 5 个记忆工具里，`read_memory` / `edit_memory` / `memory_get` 这 3 个在 jiuwen-memory 接口面找不到对应入口。三者共同视角是「按 md 文件路径操作 md 原文」（LLM 传 md 文件路径，不传 unit_id），与已有接口的「按 unit_id 操作 MemoryUnit」视角对不上。

方式 A 在 `MemoryAPI` 抽象层新增 2 个 file path 视角接口，透传到 `LocalMarkdownStore` 文件级能力：

- `read_md_file(context, md_filename, line_range)` —— 按 basename + 行范围读 md 文件原文
- `edit_md_file(context, md_filename, old_text, new_text)` —— 按 basename + old_text 在文件内做唯一替换

`memory_get` 与 `read_memory` 共用 `read_md_file`，仅入参/返回格式转换差异，下沉到 provider 层。故 3 个缺失工具实际补齐 2 类能力。

代码调研后，设计文档（`jiuwen-memory-md文件接口设计.md` §2.1）的实现路径有 4 处理想化假设与真实代码不符，本文决策已校正（见「决策 D2 / D3 / 拒绝的方案 R4 / R5」）。

## 决策

### D1 透传链路：经 Engine 窄端口 6 层穿透，不改装配

新增接口沿用现有 `add` / `get` 的委托模式，6 层穿透：

```
MemoryAPI.read_md_file / edit_md_file          ← 接口层（鉴权 + 委托）
  ↓ self._queries / self._commands
MemoryQueryService / MemoryCommandService      ← application port（薄包装）
  ↓ self._engine
MemoryEngine.read_md_file / edit_md_file       ← 编排层窄端口（协程）
  ↓ asyncio.to_thread(self._domain_store.…)
CompositeDomainStore.read_md_file / edit_md_file  ← 数据面编排（双写协调）
  ↓ self._raw_markdown() / self._raw_shadow_index()
MarkdownStore.read_content / edit_content       ← md 文件级能力（新增）
DocumentShadowIndex.find_unit_ids_by_content_hash  ← hash 反查（新增）
DocumentShadowIndex.get_units                   ← 取完整 MemoryUnit（已有）
```

**为什么不走「API 层直访 store」**（拒绝的方案 R3）：架构铁律 1「接口层不做编排」，API 层只鉴权 + 委托；`LocalMemoryAPI` 当前不持有 `StoreManager` 引用（构造函数无 `storage` 参数），所有数据操作经 `self._commands` / `self._queries` → Engine。选窄端口链路不改装配（`assembly.py` 现有构造不动），Engine 实现内部已持有 `self._domain_store`，窄端口自然落地。

### D2 edit 的 shadow 重建走同步内联，不依赖异步看门狗

设计文档 §2.1.2 说「关窗后同步触发 shadowIndex 重建（LocalWatchdog._sync_one）」。调研确认 `_sync_one` 是**私有方法**，且唯一触发入口是 inotify 文件监听 → 2s debounce → async task（`local_watchdog.py:378` / `:308-346`），进程内无公开同步入口。

若依赖异步链，`edit_md_file` 返回 `list[MemoryUnit]`（新 unit）的契约无法满足——返回时新 unit 尚未落 shadow，`shadow.get_units` 查不到。故决策：在 `CompositeDomainStore` 内**内联同步**执行 diff + delete + insert（与 `_sync_one` 同构逻辑），保证接口返回时新 unit 已落库。

写窗保护仍用现有 `open_write_window` / `close_write_window`（`sync_gate.py:58/63`）包住「md 替换 + shadow 重建」两步；关窗后看门狗的 inotify 会再触发一次 `_sync_one`，但 shadow 已同步、diff 为空，幂等无副作用（`write_window_open()` 让看门狗推迟至窗口关闭后重跑）。

### D3 content_hash 反查新增方法，复用已有 idx_content_hash 索引

设计文档说「用 content_hash 在 shadowIndex 反查 unit_id」。调研确认 `idx_content_hash` 索引已建（`sqlite_shadow_index.py:154`）但**零查询使用**——没有 `find_by_content_hash` 方法。唯一能拿 content_hash→unit_id 映射的途径是 `list_units_by_md`（返回 `[(unit_id, content_hash)]`，看门狗跨 scope 诊断用，`sqlite_shadow_index.py:890`），调用方自己在内存建 dict。

决策：在 `DocumentShadowIndex` ABC 新增 `find_unit_ids_by_content_hash(scope, content_hash) -> list[str]`，实现走 `WHERE content_hash = ? AND <scope 5 段等值>`，scope 隔离口径与 `get_units` 一致（非 `list_units_by_md` 的跨 scope 例外口径）。索引已存在，查询是 O(log n) 点查。

content_hash 口径 = `sha256(segments[0].content.encode("utf-8")).hexdigest()`，不归一化（`sqlite_shadow_index.py:500-502`）。read_md_file 侧算 hash 必须用同口径。

### D4 edit 走「继承式 SUPERSEDE」：新 unit 继承旧 unit 元数据，旧版标记 superseded

edit 的语义是「只改 segments 内容」，元数据理应保留——md 文件不存元数据（块结构只有 `# 标题\n正文\n\n`，memory_class/coords/tags/tier/user_metadata 全在 shadow 的 unit_json 里，见 `local_markdown_store.py:413-450` `_render_block`），故改 md 后新块的元数据**只能从旧 unit 继承**。

看门狗 `_sync_one` 的「删旧建新」（`local_watchdog.py:420-438`）会**丢失原 unit 的全部业务元数据**——`_build_unit`（`:444-479`）建的新 unit 只有从路径反推的 project/memory_class + `watchdog_sync` provenance + SEMANTIC tier，原 unit 的 md_title/tags/user_metadata/provenance 历史全丢。看门狗面对的是可能被改乱的文件，丢元数据可接受；edit_md_file 是受控精确替换，丢元数据是实质语义损失。

**决策**：edit_md_file 的 shadow 同步走「继承式 SUPERSEDE」，与文档模式已有的 SUPERSEDE 范式一致（`cloud_engine.py:663-689` / `agent_engine.py:560-588`：新 id + `supersedes` 指向旧 id + 旧版标记 SUPERSEDED）。

落库动作（md 文件已在 `edit_content` 阶段原子改好，shadow 同步只动 shadow 表，不再触碰 md）：

```
1. old_pairs = shadow.list_units_by_md(scope, rel_path)   # [(unit_id, content_hash)]
   old_units = shadow.get_units(scope, [uid ...])          # 拿旧 unit 完整对象（含元数据）

2. 读改后 md → 按块解析 → 新 [(content_hash, content, block_index)]

3. diff:
   superseded = old_hashes - new_hashes    # 旧块消失 → 旧 unit 标记 SUPERSEDED
   inserted   = new_hashes - old_hashes    # 新块出现 → 插入新 unit（继承元数据）
   unchanged  = old_hashes & new_hashes    # 块不变 → 跳过

4. 纯内存构造 inserted_units 整批（见 D4.1 跨块对齐策略决定继承源）：
   new = deepcopy(继承源旧 unit); new.id = uuid4(); new.supersedes = 继承源旧 unit.id
   new.segments = [Segment(content=新content)]; new.lifecycle = ACTIVE; new.t_valid = now
   （不落库，先算齐整批——见下方写失败补偿）

5. 批量落库（2 个单事务，替代原逐条 insert/update 的 N+M 个事务）：
   shadow.insert_units(scope, inserted_units)   # 事务 1：全部新 unit（新 content_hash→建 FTS5/vec0）
   对每个 superseded 旧 unit：old.lifecycle = SUPERSEDED; old.t_invalid = now
   shadow.update_units(scope, superseded_units) # 事务 2：全部标旧（content_hash 不变→只覆写 unit_json+lifecycle）
   （md 侧都不动块——edit_content 已把 md 改成最终态）

6. 返回：new_unit_ids = [u.id for u in inserted_units] → get_units 拿新 MemoryUnit list
```

**写失败补偿**（S09 第 12 条，对齐 `add`/`update`/`delete` 三路的 F08 写失败补偿）：`edit_content` 已把 md 改成 new 态、后续 shadow 同步失败时须回滚两处，让「异常等价于未发生」、同一 `old_text` 可重试（否则 retry 因 old_text 消失报 NotFoundError）——shadow 侧：insert 单事务失败则 shadow 无变化无需回滚，update 单事务失败则 `delete_units(new_unit_ids)` 删刚插的新 unit（幂等，旧 unit 因事务回滚天然 ACTIVE）；md 侧：调用方 `edit_md_file` 在 `edit_content` 前 `read_content` 抓旧全文快照，失败时 `md.restore_content(raw_old)` 回写旧全文（新增契约方法，无条件整文件覆盖、复用 `_safe_restore`，不做子串/唯一性假设）。补偿失败只记 warning、不掩盖原异常，漂移交看门狗对账。

**取舍（拒绝的方案）**：① 拒绝「可恢复提交（journal）」——md（文件）与 shadow（sqlite）跨资源做不到真原子，journal 记录 + 重放复杂度高，收益不抵成本；② 拒绝「反向 `edit_content(new_text→old_text)` 回滚 md」——`new_text` 替换后在文件里可能不唯一，反向 `str.count` 会误抛 `ValidationError`，故新增 `restore_content` 无条件整文件覆盖，不做子串/唯一性假设。

**与现有 `domain_store.update` SUPERSEDE 路径的差异**：现有 `update` 是「先改 shadow 再改 md」（`composite_domain_store.py:289-336`，`shadow.update_units` + `md.replace_content`/`remove_content`）；edit_md_file 是「md 已改，只同步 shadow」——因 `edit_content` 已原子改好整个 md 文件，shadow 同步无需再动 md。故 edit_md_file 在 DomainStore 走专用同步路径，不复用 `domain_store.add`（会重复写 md 块）或 `domain_store.update`（它假设 md 还没改、自己改 md）。

**写窗保护**：`open_write_window` / `close_write_window`（`sync_gate.py:58/63`）包住「edit_content 改 md + shadow 同步」全程；关窗后看门狗 inotify 再触发 `_sync_one`，但 shadow 已同步、diff 为空，幂等无副作用。

### D4.1 跨块对齐策略：对齐继承 + 不对齐退化兜底

edit 的 old_text 可能跨多块（含块间 `\n\n`），替换后块数可能变（合并/拆分）。按替换前后块数是否一致分两种：

**对齐场景（替换前后块数相同，按 block_index 一一对应）**：
- 每个新块按位置继承对应旧 unit 的全部元数据（system_metadata / user_metadata / tags / scope / tier / provenance）
- 新 unit 的 `supersedes` 指向对应位置的旧 unit_id
- 这是常见场景（LLM 改某条记忆正文片段，块数不变）

**不对齐场景（合并/拆分，新块数 ≠ 旧块数）**：
- 无法明确「新块继承哪个旧 unit」——跨块合并本就语义模糊
- 退化兜底：新 unit 继承文件级兜底元数据——scope 取**本次 edit 的发起者 scope**（`edit_md_file` 入参 `context.scope`，非 `latest_scope_by_md`）、project/memory_class 从 md 路径反推、provenance 标记 `md_edit_fallback`
- 不保留 `supersedes`（无明确继承源，不指向单一旧 unit）
- 这是对齐策略的退化补充，覆盖「old_text 跨多块」的完整契约，但元数据精度低于对齐场景

> **scope 取发起者而非 `latest_scope_by_md` 的理由**（实现期修正）：`edit_md_file` 有明确的发起者 scope，若退化兜底用 `latest_scope_by_md`（看门狗面对「文件被外部改乱、无明确发起者」时的口径），fallback 新 unit 可能落到他 scope，导致随后 `get_units(scope, [id])` 按发起者 scope 反查返回空、违反 `edit_md_file` 返回 `list[MemoryUnit]` 的契约。看门狗与 edit_md_file 的语义差异：看门狗是无明确发起者的被动同步、用 `latest_scope_by_md` 合理；edit_md_file 是有明确发起者的主动编辑，用发起者 scope 守住返回契约。project/memory_class 仍从 md 路径反推（路径即落点的事实记录，比发起者 coords 更可靠）。

**消失的旧块（superseded）一律标记 SUPERSEDED**，无论对齐与否——旧块从 md 消失即意味着旧记忆被编辑取代，版本链应记录这一事实。

### D5 鉴权 action：read_md_file = READ，edit_md_file = WRITE

- `read_md_file` → `Action.READ`，对 `context.scope` 一段鉴权（与 `get` / `list` 同）
- `edit_md_file` → `Action.WRITE`，一段鉴权（与 `add` 同）

**为什么 edit 不用 `Action.UPDATE`**（拒绝的方案 R6）：`update` 走两段鉴权（先按 unit_id 查真源 scope 再鉴权），但 edit_md_file 是文件级操作、不按 unit_id，两段鉴权不自然；且 edit 走「继承式 SUPERSEDE」本质是写入新记忆，语义对齐 `add`（WRITE）而非修正已有（UPDATE）。

### D6 返回 list[MemoryUnit]，MdFileContent / MdFileEditResult 转换下沉 provider 层

内核接口统一返回 `list[MemoryUnit]`（与 `add` 返回 MemoryUnit list、`search` 返回 ScoredMemoryUnit list 同构）。`MdFileContent` / `MdFileEditResult` 是为适配 jiuwenswarm 工具返回格式定义的转换产物，职责在 provider 层（`JiuwenMemoryProvider.handle_tool_call`），内核不感知下游工具契约。

内核返回 MemoryUnit list 的用途：让调用方拿到编辑后的新 unit_id（后续 search/get/evolve 需要）；read 侧让 provider 校验块边界一致性。provider 转换的具体选择（`_units_to_md_content` 选项 a 直接读 md 原文 vs 选项 b 从 unit 拼回）见 `jiuwen-memory-md文件接口设计.md` §2.5，不在本特性范围。

### D7 新建 MdOpsMixin，挂 LocalMemoryAPI 继承列表

现有 5 个 mixin 按数据面语义划分（WriteOps / QueryOps / AdminOps / SpaceOps / PepOps）。md 文件操作是新的存储访问模式（经 Engine 窄端口，非经现有 `_commands.write` / `_queries.get`），职责足够独立，新建 `memory_api_impl/md_ops.py`（`MdOpsMixin`）承载 `read_md_file` / `edit_md_file`，加入 `LocalMemoryAPI` 继承列表。

## 拒绝的方案

### R1 方式 B：adapter 层用已有接口组合翻译

内核接口面没有「按 file path 反查 unit_id」入口——`list` 的 `extensions` 入参被忽略、`filters` 是否支持 md_filename 维度不确定；`search` 走语义检索不能按 path 返回该文件所有 unit；`get` 入参是 unit_id 不是 path；`shadow_index.list_units_by_md` 是看门狗内部方法不在对外契约。且 `get` 返回 MemoryUnit（segments[0].content，被 extractor 蒸馏）与 `read_memory` 期望的 md 原文（含 frontmatter / 标题 / 块结构）语义根本对不上，adapter 无法拼回。方式 B 要落地必然要在内核加 file path 能力，与方式 A 本质重合但额外要做「MemoryUnit → md 原文拼回」的语义适配，代价更高。详见 `jiuwen-memory-md文件接口设计.md` §3、§4。

### R2 md ↔ unit_id 映射放在 agent-core 层

agent-core 的 `ExternalMemoryRail` 是纯透传机制（从 provider 拿 schemas 注册成 ability，LLM 调用时透传给 `provider.handle_tool_call`），不直接接触内核接口。无论映射放在 Rail / Provider / 直调 LocalMarkdownStore，都绕不开「内核需提供按 file path 反查能力」这个前置——agent-core 层做映射 ≠ 内核不需要加接口。方式 A 的本质就是把这个映射下沉到内核，让 agent-core 只做透传。详见 `jiuwen-memory-md文件接口设计.md` §4。

### R3 API 层直访 store（给 LocalMemoryAPI 注入 StoreManager）

给 `LocalMemoryAPI.__init__` 加 `storage: StoreManager` 参数，API 层直接调 `storage.markdown().read_content()`。违反架构铁律 1「接口层不做编排」——API 层应只鉴权 + 委托，编排逻辑在 DomainStore。且需改装配（`assembly.py:389-418`），破坏「`_Kernel.storage` 私有、Access 层只拿 `MemoryAPI`」的 S02 边界。经 Engine 窄端口链路不改装配、保持分层。

### R4 依赖异步看门狗触发 shadow 重建

`LocalWatchdog._sync_one` 是私有方法 + 无公开同步入口，触发经 inotify → 2s debounce → async task。若 edit_md_file 只写 md、靠看门狗异步重建 shadow，接口返回时新 unit 不在 shadow 里，无法用 content_hash 反查新 unit_id 返回——`edit_md_file` 返回 `list[MemoryUnit]` 的契约无法满足。设计文档两份 md 对「关窗后是否同步触发」说法互相矛盾，本决策取「同步内联重建」以守住返回契约。

### R5 复用现有 replace_content 做任意子串替换

`MarkdownStore.replace_content`（`local_markdown_store.py:185-264`）按 `\n\n` 切块、逐块比对「块正文 == old_content」命中后整块替换——它定位的是整个块的正文行，不是文件中的任意子串；且不做唯一性校验（重复 content 只改第一处）。edit_md_file 要对齐 agent-core `edit_memory` 契约（old_text 任意文本、跨块均可、必须唯一），现有 `replace_content` 不直接适用。故新增 `edit_content` 做文件级任意子串唯一替换（`str.count` 校验唯一性 + `replace(old, new, 1)` + 原子写）。

### R6 edit_md_file 用 Action.UPDATE 两段鉴权

`update` 的两段鉴权依赖「按 unit_id 查真源 scope」。edit_md_file 是文件级操作不按 unit_id，且一个 old_text 可能跨多块（影响多个 unit），两段鉴权无法自然定位「目标 unit」。且 edit 走删旧建新本质是写入新记忆，语义对齐 `add`（WRITE）。故用 WRITE + 一段鉴权（对 `context.scope` 鉴权，与 add 同）。

## 落地路径

> 本节为方案阶段过渡内容，记录逐层改动点。代码上库后，实现细节（方法体、文件关系）归各 `jiuwen_memory/<subdir>/AGENTS.md`，接口契约归 `docs/specs/S02-memory-api.md`，本节精简为「涉及哪些层」。

### 逐层改动点

| 层 | 改动 |
|---|---|
| 存储 ABC | `markdown.py` +`read_content` / `edit_content` / `restore_content`（补偿回写旧全文）；`shadow.py` +`find_unit_ids_by_content_hash`；`domain_store.py` +`read_md_file` / `edit_md_file` |
| 存储实现 | `local_markdown_store.py` +`resolve_md_path`（basename+project→路径，与 `_md_path` 同口径）/ `read_content`（行切片 clamp）/ `edit_content`（唯一性校验 + 原子写）/ `restore_content`（整文件覆盖，复用 `_safe_restore`）；`sqlite_shadow_index.py` +`find_unit_ids_by_content_hash`；`composite_domain_store.py` +`read_md_file` / `edit_md_file`（md 快照 + 失败回滚）/ `_supersede_shadow_for_md`（继承式 SUPERSEDE 同步，批量落库 + 自回滚，写窗保护，见 D4 落库动作） |
| 存储共享 | 新建 `markdown_impl/_md_blocks.py`，提取块解析逻辑（`collect_md_blocks` 返回 `[(content_hash, content, block_index)]`），看门狗 + DomainStore 共用，避免重复 |
| 编排 ABC | `engine.py` +`read_md_file` / `edit_md_file` 协程 |
| 编排实现 | `cloud_engine.py` / `agent_engine.py` +实现 +`_project_from_context`（从 `context.extensions["coords"]["project"]` 取 project）；`in_memory_engine.py` 抛 `ValidationError`（非文档模式） |
| App Port | `application/query.py` +`read_md_file` 委托；`application/command.py` +`edit_md_file` 委托 |
| 接口 ABC | `memory_api.py` +2 个 `@abstractmethod`（签名见 `jiuwen-memory-md文件接口设计.md` §2.1.1 / §2.1.2） |
| 接口实现 | 新建 `memory_api_impl/md_ops.py`（`MdOpsMixin`）；`local_memory_api.py` 继承列表 +`MdOpsMixin` |

### md 路径解析（basename + project → 磁盘路径）

`resolve_md_path` 与现有 `_md_path`（`local_markdown_store.py:339-371`，按 memory_class 映射）产出同一路径，但入参是 basename + project（不依赖完整 MemoryUnit）：

| basename | project | 路径 |
|---|---|---|
| `USER.md` | 任意 | `USER.md`（root 顶层，跨 project，不进 memory/ 无 project 段） |
| `MEMORY.md` | `p1` | `memory/p1/MEMORY.md` |
| `MEMORY.md` | `""` | `memory/MEMORY.md`（跨项目可见） |
| `2026-09-30.md` | `p1` | `memory/p1/daily_memory/2026-09-30.md` |
| `2026-09-30.md` | `""` | `memory/daily_memory/2026-09-30.md` |

路径安全由现有 `_safe_abs_path`（`local_markdown_store.py:454-489`，realpath + commonpath 校验不逃逸 root）收口，basename 逃逸 `..` / 绝对路径抛 `ValidationError`。

### read_md_file 实现路径

1. 解析 basename + project → md 路径，读原文按 line_range 切片（越界 clamp）
2. 按块解析切片内容（`\n\n` 分隔，每块 `# 标题` + 正文行）
3. 对每个完整块的正文算 content_hash（sha256，与 shadow 同口径）
4. `shadow.find_unit_ids_by_content_hash(scope, hash)` 反查 unit_id
5. `shadow.get_units(scope, [unit_id, ...])` 拿完整 MemoryUnit list 返回（被 line_range 切断的不完整块 hash 匹配不上，自然跳过）

### edit_md_file 实现路径

1. 解析 basename + project → md 路径
2. `open_write_window()`
3. `md.read_content(...)` 读旧全文做快照（既算块 diff 又留 md 补偿数据）
4. `md.edit_content(...)` 文件级唯一替换（原子写）——md 文件已改为最终态
5. `_supersede_shadow_for_md(scope, rel_path)` 同步重建（继承式 SUPERSEDE，见 D4 落库动作）：
   - `list_units_by_md` 拿旧 (id, hash) → `get_units` 拿旧 unit 完整对象（含元数据）
   - 读改后 md 按块算新 (hash, content, block_index)
   - diff：superseded（旧块消失）→ 标 SUPERSEDED；inserted（新块出现）→ 按 D4.1 跨块对齐策略继承旧 unit 元数据构造新版（uuid4 新 id，supersedes 指向继承源）
   - 纯内存构造整批 inserted_units → 批量 `insert_units`（单事务）→ 批量 `update_units`（单事务）
   - md 侧不再触碰（edit_content 已改好）
6. shadow 同步失败时补偿（见 D4 写失败补偿）：shadow 侧 `delete_units(new_unit_ids)` 删已插新 unit；md 侧 `restore_content(raw_old)` 回写旧全文；再重抛原异常
7. 成功返回 `get_units(new_unit_ids)`；`close_write_window()`（finally）

> **md 侧零触碰**：步骤 4 后 md 已是最终态，步骤 5 只动 shadow 表（标旧版 + 插新版），不再调 `md.write`/`md.replace_content`/`md.remove_content`——这是与现有 `domain_store.update` SUPERSEDE 路径（先改 shadow 再改 md）的根本差异，因 edit_md_file 的 md 改动在 edit_content 阶段就完成了。

## 验证

> 待实现后填充实测结果。测试路径镜像源码，存储后端用内存实现。

| 测试文件 | 覆盖 |
|---|---|
| `tests/unit/storage/test_local_markdown_store.py` | `read_content` 行切片 / clamp；`edit_content` 唯一性校验 / NotFound / 原子写回滚；`resolve_md_path` 4 种 basename×project 组合与 `_md_path` 一致性 |
| `tests/unit/storage/test_sqlite_shadow_index.py` | `find_unit_ids_by_content_hash` scope 隔离（5 段等值）、索引命中、跨 scope 不泄漏 |
| `tests/unit/storage/test_composite_domain_store.py` | `read_md_file` 块→hash→unit 反查；`edit_md_file` 继承式 SUPERSEDE（元数据继承 + 版本链 `supersedes` + 旧版标记 SUPERSEDED）+ shadow 同步 + 写窗保护；跨块对齐场景（块数一致按 index 继承）+ 不对齐退化兜底场景（合并/拆分）；非文档模式拒绝 |
| `tests/unit/api/test_local_memory_api_md_ops.py` | 鉴权（READ / WRITE）、拒绝非文档模式、剥瞬态键、`MdOpsMixin` 无同名方法重复 |

## 已知遗留

1. **edit 继承式 SUPERSEDE 的版本链膨胀**：每次 edit 都生成新 unit + 旧版标记 SUPERSEDED（非物理删）。频繁编辑同一块会累积 superseded 旧版（shadow 表行数增长）。与现有 `update` SUPERSEDE 路径同款代价，由 `sweep_expired` 清理过期旧版（lifecycle 治理）。若后续发现高频 edit 场景版本链膨胀过快，可考虑对 edit 路径加「连续 edit 同一块合并为新版」优化，但首版不做。
2. **不对齐场景的元数据精度低于对齐场景**：跨块合并/拆分时，新 unit 退化为文件级兜底元数据（`latest_scope_by_md` 取 scope + 路径反推 project/memory_class + `md_edit_fallback` provenance），不保留 `supersedes` 版本链。这是跨块语义模糊的固有代价——old_text 跨多块时无法明确归属单一旧 unit。覆盖完整契约（对齐 + 不对齐），但不对齐场景的元数据保真度有限，后续若有更精细的跨块归属算法可迭代。
3. **provider 层转换不在本特性范围**：`MdFileContent` / `MdFileEditResult` 转换、`_units_to_md_content` 选项 a/b 选择，归 provider 层（`JiuwenMemoryProvider` / `ExternalMemoryRail`），本特性只交付内核接口 + 返回 `list[MemoryUnit]`。
4. **看门狗与 edit_md_file 两套构造逻辑不合并**：`collect_blocks_from_text`（原看门狗 `_collect_new_contents`）/ `_build_unit` 提取到 `_md_blocks.py` 共享模块后，看门狗的 scope 继承（`latest_scope_by_md`）与「删旧建新」逻辑须一并保留（看门狗面对任意文件修改，仍走删旧建新、不继承元数据——edit_md_file 的继承式 SUPERSEDE 是 DomainStore 专用路径，不改造看门狗），避免回归。
5. **InMemoryEngine 不支持**：read_md_file / edit_md_file 仅文档模式可用，`InMemoryEngine` 抛 `ValidationError`。若未来 InMemoryEngine 需支持，需另议非文档模式的 md 文件语义（当前非文档模式真源是 KV，无 md 文件）。
6. **line_range 切断块的 hash 匹配**：read_md_file 按 line_range 切片后，被切断的不完整块算出的 hash 匹配不上 shadow 里的完整块 hash，自然跳过——这是预期行为（只返回完整块对应的 unit），但 provider 层转 `MdFileContent` 时若需要切断块的原文，应直接读 md 原文而非依赖 MemoryUnit list（见 `jiuwen-memory-md文件接口设计.md` §2.5 选项 a）。
