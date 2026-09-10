# F08 — MemoryUnit 树结构

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-15 |
| 影响范围 | jiuwen_memory/api/、jiuwen_memory/common/、jiuwen_memory/construction/、jiuwen_memory/control/、jiuwen_memory/ingest/、jiuwen_memory/retrieval/、jiuwen_memory/storage/；docs/specs/S01–S07；关联 [`F05-construction-spec-multimodal-design`](../construction/F05-construction-spec-multimodal-design.md) |
| 测试基线 | 阶段 1：完整 unit 回归 1891 passed；阶段 2：2082 passed；阶段 3：2229 passed；阶段 4：2413 passed；阶段 5：2505 passed；阶段 6：2585 passed；阶段 7：2665 passed；阶段 8：2754 passed。各阶段均为 5 skipped、480 deselected。各阶段变更无新增 Ruff/CodeCheck 本地预审问题，历史诊断见对应阶段验证；未执行云端 CodeCheck |
| Refs | — |

## 阶段 1 落地（2026-09-10）

本次只交付“能表达、接入和存储结构身份”的基础能力，不交付自动建树或层级召回。
本节记录阶段 1 完成时的状态：当时除本节明确列出的能力及下文叶提示接入外，Composer、
显式 `evolve(HIERARCHY)`、结构查询/展开/MaxP、后台任务、修复器和各 kind 算法均未实现。
当前新增交付以阶段 8 小节为准；后文仍保留尚未开放的总体设计。
后文 P0–P5 是原设计分期，不等同于已经完成的提交阶段。

### 已交付与决定

- `MemoryUnit` 内嵌默认空的 `HierarchyRef`，包含 kind/role、直接父子引用及可选
  Scope、覆盖区间、ordinal 与结构状态。五种 kind、七种 role 只是通用词表，
  不代表五种树算法已可用，也不替代 L0/L1/L2、tier 或多模态构建粒度。
  `hierarchy` 追加在既有 `vectors` 字段之后，保持原有位置参数顺序。
- 提供 `validate_ref` / `validate_tree` 两个纯函数；前者检查单引用，后者先检查输入
  引用字段，再检查非空结构集合的单 kind、Scope、单父、双向引用、环与区间覆盖。不读取全库，
  不排序；集合外邻居不自动补齐。普通 Ingestor 与 codec 不自动调用这两个函数。
- codec 保持 `_v=4`，保留 vectors、双 metadata 与系统瞬态键剥除。非空 hierarchy
  写出，缺少该字段的 `_v=4` 数据读为空结构；未知枚举或时间解析失败降级为空结构
  并留诊断，不改变 `_v<4` 必须先离线迁移的要求。
- 只消费 `system_metadata` 中四个叶提示，生成无边的叶引用；未知前缀键、边提示和
  父角色被拒绝，`user_metadata` 不解释。SimpleIngestor 不落盘，CloudEngine 不重新
  回注已消费提示，普通 infer/procedural 分流保持原状。
  仅索引拥有的 `hierarchy_status`、`parent_id`、`span_start`、`span_end` 与叶提示
  不同：公开 add/update 沿用系统保留键校验明确拒绝这些输入，业务同名字段应放入
  `user_metadata`，不能接受后再由索引静默丢弃；不新增 API。
- 全文、向量及一体化写路径投影 `hierarchy_kind`、`hierarchy_role`、`hierarchy_status`、
  `parent_id`、`span_start`、`span_end` 六键，区间使用 UTC epoch 毫秒。
  build/update 清除旧投影后以当前引用重新生成；全文/向量不把来自 Unified 的旧系统
  六键副本继续复制为 `system_metadata.*` 索引字段。一体化路径的系统元数据副本
  不取代 hierarchy 真源，切换 builder 后也只认当前结构。

### 本阶段不采用的方案

- 不为新增可缺省字段提升 codec 版本：它不改变既有字段含义，缺失可安全读为空；
  但不借此放开旧混合 metadata 的兼容边界。
- 不用用户元数据或任意 `hierarchy_` 输入建边：来源只能声明叶身份，不能绕过未来
  构建边界制造父子关系；已消费提示也不重复保存在系统元数据中。
- 索引不用 ISO 字符串做范围比较：现有 FilterClause 范围值要求数值，UTC 毫秒与
  既有时间索引一致。KV 仍保留 ISO 时间，不为后端反写核心字段。
- 不提前把纯校验插进全部写入链或引入定时/查询入口：阶段 1 保持现有编排，
  Composer 的提交前校验与其他树能力由后续阶段分别交付。

### 验收范围与已知限制

本阶段验收覆盖引用字段与独立默认值、纯校验正反例、codec 往返与 `_v=4` 无字段
兼容、vectors/双 metadata/瞬态键共存、四种合法提示字段及拒绝路径、Cloud 消费后
不回注，以及全文/向量/一体化 build/update 的六键投影与旧值清理；同时覆盖系统索引
专用键前置拒绝、用户同名值保留，以及 Unified 来源切换到全文/向量后的旧副本清理。
验证命令：`PYTHONPATH=.:jiuwen_memory_entry/core python -m pytest -p no:cacheprovider -m unit`。
本轮结果为 **1891 passed、5 skipped、480 deselected**；5 个跳过项是原有真实 LLM / Redis
用例，另有 5 条原有 `pytest.mark.asyncio` 未注册警告。HTTP 用例需要允许绑定本地回环
端口；解除测试沙箱的端口限制后，完整 unit 回归通过。变更 Python 文件 Ruff 与
`git diff --check` 通过。本地 CodeCheck hygiene 预审整理了测试构造参数及公共函数
说明，未执行 GitCode 云端 CodeCheck。

`infer=true` 不保证派生节点继承 hierarchy：KeywordExtractor 的深复制与 LLM 类
Extractor 新建 unit 的行为尚未统一。纯校验通过不等于全库引用可解析；结构词表也
不承诺角色链算法约束。`IndexBuilder.rebuild()` 仍未实现恢复，六键投影不等于公开
结构过滤、真源复核或层级召回已经打通。以上均是本阶段保留边界，不扩展为本轮修复。

## 阶段 2 落地（2026-09-10）

本节记录阶段 2 完成时的状态：补齐“显式给定 snapshot → 生成 time_span → 校验 →
保存或替换父层”的最小闭环。当时它是内部构建能力，不是公开建树任务功能；MemoryAPI
和 Engine 对 HIERARCHY 仍明确拒绝，普通 add/write 不自动建树。

### 决策与交付边界

- 内部演进调用统一为 EvolveRequest，HIERARCHY 将已提供节点按叶/父角色分开，直接
  委托 Composer 做有界替换；没有旧父也可首次建树。不进入抽取或去重，不按 infer
  参数重新筛选。其余四种演进模式保持原行为。依赖和阈值分别用
  EvolverDependencies / EvolverOptions 聚合，历史 YAML 配置不迁移。
- Composer 只处理调用方提供的叶、旧父、tree home scope 和区间，不扫描全库。首次
  build 拒绝覆盖已有父关系；replace 要求已知旧父的全部直接子叶齐全，带父边的叶
  必须提供其旧父。边界切过旧父时，调用方可显式提供区间外的完整子集，缺子就拒绝，
  不自行扩大数据库查询。这个边界使“范围选择”和“树结构构建”可以分别验收。
- 候选在深拷贝上生成，提交前检查字段、完整 Scope+id、租户/主体边界、双向引用、
  单父、无环与覆盖区间。校验失败零写入、不污染输入对象。session 可跨；org+space
  不能跨，不同非空 user/agent 默认拒绝，只有构造配置显式允许才放开。
- TIME 仅有 snapshot→time_span。按 UTC span_start、t_event、原输入序稳定排序；
  相邻 session 或配置系统上下文变化、相邻 span 间隔大于阈值时切段。默认两小时，
  等于阈值不切，限制的是相邻间隔而非整组时长；内容主题变化本身不构成判据。
- 父正文采用时间/数量表头加有限原文摘录，不称为完整语义摘要。每组产生新 UUID 的
  EPISODIC/time_span，覆盖全部直接子区间并保存有序 Scope 引用；叶正文、时间、tier、
  来源与生命周期不变，包含关系不写 provenance 或 supersedes。
- 父用户元数据遵循现有相等交集规则并深拷贝；系统元数据只上提配置的一致非空字符串
  键及请求额外 metadata，同名时一致子值优先；不全量继承，infer/procedural/middle 不传播。实体保序合并；
  不借建树重设计作者身份或访问权限继承。
- Composer 注册为独立可选构建依赖，只有 Evolver 显式引用时才装配。两者必须显式
  引用同一个具名 IndexBuilder，避免建树写入另一套真源。profile 只接受 TIME 两层
  配置，未提供 profile 时用规则默认值；未知配置、LLM、scene/event、settle 均拒绝。
- 保存顺序是新父本体 → 子边 → 旧父归档清边 → 旧父软删索引 → 新父与叶刷新索引，
  全部经 IndexBuilder 的 FORWARD_ONLY / RETRIEVAL_ONLY / SOFT，不重复插入叶本体。
  本体阶段失败即停，索引阶段逐项报告；complete=false 不代表回滚完成，也不触发自动修复。

### 本阶段拒绝的方案

- 不从 Composer 查询或“猜齐”缺失叶：自动扩大范围需要控制层读取与并发边界，不能
  用部分输入构造出表面合法、实际丢失旧子关系的树。
- 不提前接公开 HIERARCHY 任务、自动派生或召回：本阶段先验证明确输入上的保存契约，
  不把类型存在当作鉴权、调度与范围查询已经完成。
- 不引入 scene/event、模型摘要和父 L0/L1：规则摘录已能验证树结构，不用未交付的
  模型配置假装支持更高层算法。
- 不复用旧父 id、不硬删旧父或叶：父替换通过新 id 和归档表达；结构关系与版本血缘分离。
- 不宣称事务和自动恢复：现有 IndexBuilder 没有全流程事务返回契约，批次失败可能
  已部分写入，只能报告不完整状态并保留修复线索。

### 验证与遗留

阶段 2 测试覆盖排序与 UTC/微秒边界、相邻间隔和上下文分组、有限摘录、父范围与引用、
元数据边界、输入不变、首次构建/连续重建、缺子拒绝和各保存阶段失败；同时回归原四种
演进模式与构造配置。完整 unit 回归结果为 **2082 passed、5 skipped、480 deselected**
（62.92 秒），保留 5 条既有 asyncio marker 警告。新增专项合计 191 例：TIME 规则 76、
Composer 70、Evolver/Factory/API/Engine 接线与边界 45；另对 12 个既有迁移测试文件
定向回归，164 passed。命令沿用阶段 1 的 unit 回归命令。

新增生产/测试 Python 文件与修改的生产文件 Ruff 检查通过；既有 4 个测试文件保留
22 条 HEAD 已存在的诊断（20 条 E501、2 条 F841），本次新增诊断为 0，未扩大范围
修复历史问题。按已有 10 条 CodeCheck 规则完成本地预审，整理了构造参数、公共函数
说明、同名遮蔽、无返回值调用与测试辅助断言，未发现新增风险；`git diff --check`
通过。以上不代表 GitCode 云端扫描通过，也不代表未交付能力已验收。

公开任务入口、自动范围读取、并发冲突控制、自动修复、结构生命周期联动、父 L0/L1、
scene/event、其他 kind、层级召回与预算仍未交付。普通 EXTRACT 不同实现对 hierarchy
的继承差异、IndexBuilder 全量 rebuild 恢复能力也没有在此阶段统一。

## 阶段 3 落地（2026-09-10）

本阶段把“内部能够构建 snapshot→time_span”打通为“外部可显式提交范围，由系统
收齐输入、执行建树并查询真实结果”。树层级和 TimeSpanMerger 算法不增加；显式提交
后可由调度器后台执行，不等于系统会自动触发建树。

### 决策与交付边界

- 公开演进使用统一 EvolveTaskOptions，模式、通道和可选建树参数归入同一请求对象，
  避免继续增加形参。MemoryAPI、CommandService、Engine 以及 HTTP/CLI 同步迁移，
  不保留旧独立 mode/channel 的调用形式；原四种模式的算法不变。内部 EvolveRequest
  保持原契约，仍由 Job 传入已经收齐的 MemoryUnit，而不是接收外部原始字典。
- 显式建树经过 API 的 WRITE 和 UPDATE 双重授权及空间 UPDATE 判权，并要求空间
  可写、hierarchy.enabled 已开启。默认策略关闭，只改开关不自动提交任务。
  permission.routing_fields() 非空时拒绝公开 HIERARCHY：本阶段尚未提供候选逐条
  PEP，不能让仅 Scope 授权绕过按 memory_type/pipeline 路由的细粒度权限。
- 要求任务范围与父驻留 Scope 完全相同。收叶只在同一 org/space 内展开 home 中的
  空 user/agent/session 维度；非空维度继续约束。多 session 可在一次任务中被收齐，
  但 TimeSpanMerger 按 session 分组，因此单个 time_span 不跨 session；父统一驻留
  指定 home。跨主体能否建边仍由 Composer 原有配置校验，不因范围较宽自动放开。
- 专用 HierarchyJob 收集 `/memory/` 中窗口内尚未挂父的 ACTIVE TIME snapshot，
  以及 home 下与窗口相交的旧 time_span；旧父的全部直接子叶均补齐，包含窗口外
  子叶。不按 infer true/false 筛选，不读取 `/messages/`，不重新调用 Extractor。
- 读取必须完整分页，旧父子引用先检查 Scope 边界再分批点读。重复分页键、总数变化、
  提前空页、超限、缺子、非法身份或反向引用不一致都使任务失败；不截断、不静默跳过，
  避免只用“看见的部分”覆盖旧父。父子身份始终使用完整 Scope 与 id。
- Engine 在提交时传入自身的 Evolver 与 KV，JobFactory 不另装一套 Evolver。
  Job 固定一次性，保护参数控制页大小、叶数上限和锁等待；它们不改变时间切分规则。
  调用参数在边界深拷贝，调用方提交后再修改对象不会改变已捕获的范围。
- 可选共享锁覆盖取数、候选补齐和 Composer 写入，以 home+kind 串行化显式建树。
  失锁和超时报告失败；取消时等待已启动的同步线程工作结束后才释放锁。未装配锁时
  不声称互斥，锁也不覆盖普通 write/update/delete，不提供数据库快照或事务。
- Job 使用独立 hierarchy_result 判定成功：complete=false 或任何 repair 均为 FAILED。
  Scheduler 保留 Job 返回的 SUCCEEDED/FAILED/CANCELLED 和业务 detail；非终态返回
  作为执行契约错误转为 FAILED。已有周期任务仍按 is_done 控制停止，不把业务失败
  伪装成成功；建树不会因此获得周期自动执行能力。

### 本阶段拒绝的方案

- 不只增加薄接口却让调用方继续手工传所有 MemoryUnit：公开入口必须承担有界完整
  取数，否则跨窗口旧父的完整性仍取决于外部调用纪律。
- 不用新的可选形参保留两套公开协议：统一请求对象的兼容性代价明确，Python 与
  HTTP/CLI 一起迁移，不通过任意 kwargs 隐藏接口或 CodeCheck 参数问题。
- 不把父引用当作权限凭证：越出 home 的 child Scope 必须在读取前拒绝；尚不支持
  逐条候选路由权限时明确禁用这一组合，而不是选更宽松的默认策略。
- 不截断后建树、不忽略 repair：完整性和真实失败比“任务返回成功”更重要。
  候选读取错误发生在写入前；Composer 部分失败则如实报告可能已有写入。
- 不将后台调度等同于写入自动派生，不顺带接 scene/event、查询展开、上卷或自动修复。

### 验证与已知限制

阶段 3 完整 unit 回归结果为 **2229 passed、5 skipped、480 deselected**（74.21 秒），
较阶段 2 增加 147 个通过项。新增覆盖分为 API 建树与权限 38、Job 候选/锁/结果 52、
Scheduler 终态 22，以及协议转换和调用边界 35。沿用前两阶段 unit 验证命令；5 个跳过
项为 4 个真实 LLM 用例及 1 个 Redis 双实例用例，另有 5 条原有 asyncio marker 未注册
警告，不计作新增回归问题。

验证覆盖统一请求的类型/协议拒绝、API 权限
与策略闸门、Engine 同源注入、完整分页、跨 session 收叶、跨边界拒绝、旧父全子补齐、
限额、可选锁、无候选、部分失败、Scheduler 真实终态及“写叶→显式建树→查询状态→
再次重建”的闭环，同时回归普通四种演进模式。

对本次 40 个修改/新增 Python 文件执行已有 10 条 CodeCheck 本地规则检查，无新增
结构风险；Ruff 中 39 个文件通过，另 1 个 adapter 文件保留与 HEAD 完全相同的
13 条历史 E501，本次新增诊断为 0。`git diff --check` 通过。这是变更范围的本地预审，
不是全仓 Ruff 零告警或 GitCode 云端 CodeCheck 通过；第二阶段记录的 22 条历史
诊断仍作为当时基线保留，不因本轮检查集合不同而宣称已消失。

本阶段不交付自动派生、周期建树、召回时 ensure、层级查询/展开/上卷、scene/event、
其他 kind 算法、结构生命周期联动、父 L0/L1 或自动修复。不新增父作者和可见性继承
规则；统一结构读取不代表已完成所有细粒度权限继承。

可选锁只协调采用同一锁协议的显式建树；同数量的并发内容变化不保证被分页校验发现。
修复项不是事务回滚证明。async_timer 需要持续事件循环，当前同步 API/HTTP 的临时
asyncio.run 生命周期问题不在本阶段修复；默认 in_process 在提交中等待执行。
EXTRACT 的 hierarchy 继承差异和 IndexBuilder 全量 rebuild 恢复能力也仍未统一。

## 阶段 4 落地（2026-09-10）

本阶段把“树已写入且带结构索引”变成“可按树类型、单一节点角色和覆盖区间检索”。
仍只有 snapshot→time_span 两层建树，不增加树层级或自动维护。

### 交付与调用方式

- 公开入口统一为 `search(query, context, options=None, *, security)`；
  `SearchOptions` 从 `jiuwen_memory.api` 导入，容纳既有 filters/as_of/top_k/disclosure/
  with_trajectory，以及 hierarchy_kind/hierarchy_role/span_start/span_end。
  旧平铺选项关键字是显式破坏性迁移，HTTP/CLI 使用嵌套 `options`，不维护两套协议。
- 四个结构条件经 `RetrievalQuery` 进入检索；Retriever 在 parse 后强制回填，
  自定义 Parser 不能丢失或改写。kind/role 必须为对应枚举，role/span 要求 kind，
  span 必须成对且有序。结构 span、event-time、valid-time 是三条独立时间轴。
- kind、ACTIVE 状态、可选单角色与结构区间作为外层 AND 与用户/权限/时间谓词合并；
  全文/向量（含 L0/L1/L2 入口及内存后端）在 top_k 前过滤。六个结构索引裸键不再
  被 normalize 改写成用户字段；用户同名键须显式写 `user_metadata.<key>`。
- 三种检索路径及关键词实体扩展共享 `MemoryUnit.hierarchy` 真源复核，拒绝陈旧
  metadata 伪造的 kind/role/status/span；闭区间端点相等算相交，朴素时间按 UTC，
  真源保留微秒。TIME 查询可不指定窗口，但节点本身必须有有效 span。
- 两种 Discloser 返回真实 `parent_id`，默认空串。父/叶仍按其驻留 Scope 查询，
  不自动扩大到 session 子 Scope；没有 author/coords 的派生父也不会因此绕过权限。
- API 保留 READ 鉴权、路由谓词、跨空间逐空间判权与预算；extensions 的标准容器
  复制后透传，不透明运行时插件保留身份。typed 层级请求受默认关闭的
  `hierarchy.enabled` 控制；普通 search 不增加该策略依赖。

例如查询 TIME 的 `time_span`，返回的是内容相关的时间段父节点；查询 `snapshot`
返回叶。省略 role 则同 kind 的可见活动节点都能竞争结果，仍需文本相关性，不是全树
枚举。父内容不会自动附带子全文，也不因“是父节点”获得额外分数。

### 取舍与未交付边界

- 采用一个 SearchOptions，不新增一组平铺参数、列表角色参数或 extensions 暗约定。
  与第三阶段统一 EvolveTaskOptions 的方式一致，单/跨空间只装配一份查询骨架。
- 通用 FilterExpr 是底层字段过滤能力，不自动开启 typed hierarchy 的完整语义；
  `hierarchy.enabled` 不承担访问控制。权限仍由既有 API/Storage 机制决定。
- 真源复核只防错召，不恢复被陈旧索引或 top_k/limit 丢掉的候选。图通道不下推结构
  谓词；第三方 RETRIEVE 再次复核、TIME 无窗口的节点区间有效性、毫秒投影边界附近
  的微秒排除都可能减少返回条数，不能宣称任意后端始终给满 top_k。
- 不新增 expand、rollup、ensure、scene/event、自动演进、父权限继承或重建恢复。
  L0/L1/L2 仍是每个节点自己的披露层，不是树的三层。

### 验证

新增确定性测试覆盖参数、真源/索引冲突、三检索路径、两内存索引、实体扩展、两种
Discloser、单/跨空间权限、真实写入→建树→查询和 HTTP/CLI 嵌套协议。
最终完整 unit 回归 **2413 passed、5 skipped、480 deselected**（78.87 秒），比阶段 3
增加 184 个通过项。5 个跳过仍为原有真实 LLM 与 Redis 外部依赖用例；5 条 warnings
仍为历史 `pytest.mark.asyncio` 未注册提示。沿用前述 `-m unit` 命令，HTTP/CLI 测试
允许本机回环监听；没有外部模型或正式 benchmark 运行。

独立审查在首轮测试通过后补出了普通查询兼容用例：无 `t_valid` 的历史查询、
`id/unit_id` 别名、`t_message` 字段过滤。分别以 NOT(GT) 保留无起始界、规范化别名、
补齐非空时间投影修复，不放宽通用 metadata 比较语义；后两轮完整回归均通过。

依照已有 10 条 CodeCheck 规则复查代码、测试、fixtures/stubs 及相邻定义，修正新
推导式换行与多行签名缺简短 docstring 的问题。当前累积工作区（包含阶段 3 未提交
内容）有 75 个修改/新增 Python 文件：73 个 Ruff 通过，另外 2 个保留 27 条历史
诊断（adapter 的 13 条 E501；真实 LLM 测试的 4 条 E402、3 条 E501、7 条 F541），
相对 HEAD 无新增 Ruff 诊断。已执行限定文件 `ruff check --fix`、复查及
`git diff --check`。相邻既有多参数接口等未扩大重构范围，不宣称全仓 CodeCheck 清零。

未验证真实 Milvus/Elasticsearch/Postgres 后端或 GitCode 云端 CodeCheck；尤其 Milvus
JSON 缺键在 NOT 范围谓词中的版本行为仍需真实后端验证。原有 adapter 的非 search
旧协议与全量 rebuild 等遗留未改变。

## 阶段 5 落地（2026-09-10）

本阶段把“只找到树节点”推进到“命中概要后按结构读取证据”。建树仍只有
snapshot→time_span 两层；展开算法可遍历已有合法的多层引用，不代表更高层 Composer
已经交付。生产代码、确定性测试与本阶段文档一起交付，不创建提交。

### 已交付与决定

- SearchOptions 新增 expand_depth=0，并沿已有 Python/HTTP/CLI 嵌套请求下传。0 保持
  直接命中；非零要求正整数和显式 kind，不接受 bool。沿用 hierarchy.enabled 闸门，
  没有新增公开 expand 方法、独立树预算或自动建树入口。
- DefaultExpander 由 PipelineRetriever 注入同一 DomainStore，按父声明顺序 BFS 点读。
  点读前检查 Scope，节点身份用完整 Scope+id；真源核对 kind、ACTIVE 结构状态、
  反向父引用及 span 覆盖。子保留业务/权限过滤、valid-time/event-time/结构窗口，
  仅移除 typed 父角色，不根据父可见就跳过子可见性检查。
- top_k 先选直接命中根，子不占根名额。根先消耗预算，再按根顺序分别 BFS；结果为
  所有准入根、第一根的后代、第二根的后代等。子继承根分，不另查索引、不独立评分，
  不实现 rollup/MaxP/top-M。叶根合法，depth 上限本身不算截断。
- 根与后代共享既有 max_tokens；Discloser 先生成实际字段，再按主字段
  max(1, ceil(字符数/4)) 准入。固定层级不降级；ADAPTIVE 有限预算按 L2→L1→L0
  选当前可容纳级别，无上限以 L1 为主。L0 兜底只计实际摘要而非原始长正文。
  三层字段仍同时返回，所以不是整个响应体的 token/字节硬上限。
- 跨空间先延迟展开：各空间只返回物化根，沿既有规则合并选根后统一收尾。未选根
  不读后代；预算与 1000 个子引用尝试上限全局共享（单批最多 64 个）。内部
  PreparedRetrievalResult/来源依赖不进入公开响应，defer_expansion 不对外开放。
- 缺子、坏边、不可见、读取失败和截断保留健康兄弟，问题码稳定去重，经 errors 的
  HIERARCHY 标记返回；不开轨迹也可见。开启轨迹记录 parent_recall 和逐根 expand，
  展示请求/实际深度、数量、complete/truncated 和累计估算成本，不回显被排除子内容。

例如 time_span 概要为“讨论了数据库超时调整”，两个 snapshot 分别记录问题现象与
“超时从 500ms 改为 2000ms”。搜索 time_span、top_k=1、expand_depth=1 时，预算
充足返回概要及两个 snapshot；depth=0 仍只有概要。若关键父本身未被召回，本阶段
不会凭子相关性找回该父，那是后续上卷阶段的职责。

### 拒绝的方案与边界

- 不在各空间先展开再对扁平列表 top_k 截断：那会浪费未选父的读取、挤掉根或其证据，
  并把一个预算错误地复制成多份。使用内部准备/收尾协议保留来源。
- 不用原始 content 长度冒充 L0/L1 成本，也不把树选择职责塞进 Discloser；选择器
  消费其已渲染字段，只有本阶段展开路径新增统一准入，普通检索保持既有行为。
- 不声称父相关就意味着每个子相关；当前仅沿边取证并继承分数，后续再独立实现
  子评分、MaxP 与 top-M。没有 scene/event 构建、周期演进、ensure 或自动修复。
- MultimodalRetriever 的多分支 RRF 包装尚未适配延迟展开，正深度明确拒绝。
  基础 PipelineRetriever 的三种存储执行路径均支持，不把包装器静默降级为平面结果。
- 内部身份按 Scope+id 正确区分，但公开 RetrievedItem 仍只有裸 unit_id/parent_id，
  不能据此无歧义重建跨 Scope 同名节点的整棵树。没有新增权限继承或事务快照保证。

### 验证

覆盖三条存储路径、关键词/向量父召回、父 top_k 与后代名额、深度/BFS/同名 Scope、
双向边与范围隔离、历史生命周期、独立时间轴、坏分支、读取失败、共享预算及 L0
长正文兜底；真实 API 验证两引擎写入→建树→展开、跨空间、策略、HTTP/CLI 共享序列化。
完整 unit 回归 **2505 passed、5 skipped、480 deselected**（78.50 秒），比阶段 4
新增 92 个通过项。5 个跳过仍为原有真实 LLM/Redis 外部依赖用例，5 条 warnings 为
原有 pytest.mark.asyncio 未注册提示；未运行真实后端、模型、正式 benchmark。

本阶段 22 个修改/新增 Python 文件均通过 Ruff（含 --fix 后复查）。依照已有十条
CodeCheck 规则扫描生产、测试、fixture/stub 与相邻定义：修正新增长行、导入顺序和
回调参数遮蔽风险，未见新增本地预审问题；赋值调用逐项核对返回契约，未用业务 assert
或测试 protected-access。相邻既有定义仍有 3 处多参数、6 处多行签名/docstring、
4 处可静态化方法风险，均对比 HEAD 确认为原有项，不扩大本阶段重构。
git diff --check 通过；未执行 GitCode 云端 CodeCheck，本地检查不代表云端批准。

## 阶段 6 落地（2026-09-10）

本阶段交付“叶或中间节点命中后，可以找回原本没有直接命中的父，并提高父的相关性
得分”。建树仍是 snapshot→time_span 两层，未引入 scene/event 构建或自动演进。

### 做了什么

- 统一 `SearchOptions.rollup=False`，API、HTTP/CLI 共用协议和内部 RetrievalQuery
  透传；严格 bool，开启要求显式 kind，仍受 hierarchy.enabled 门禁。
- 上卷时只取消 typed 输出角色的候选下推，父/后代使用同一个融合、精排池。精排之后、
  最终阈值及 top_k 之前，沿双向可核验的父边准入祖先。指定 role 取最近匹配祖先；
  不指定 role 保留直接命中，并额外准入直接父，不继续上卷新父。
- MaxP 取父自身与命中后代最终得分中的最大值，不累加。新父没有独立索引命中时，
  不复制子 evidence 冒充父命中。原来就命中的父保留自身 evidence。
- 上卷遍历检查完整 Scope+id、kind、状态、时间、权限/业务过滤、双向引用和 span
  覆盖；先判断 Scope 再读。单链 32 跳，单 Pipeline 调用共 1000 次父引用尝试，
  请求内按完整身份缓存；坏分支只读跳过，HIERARCHY/rollup 错误不依赖轨迹开关。
- 内置 Discloser 使用物化候选自身 unit，保留同 id 不同 Scope 父正文；展开准备按
  顺序一对一绑定根和来源。跨空间先各自上卷、再全局选根；expand_depth 独立控制
  向下读取，阶段 5 的根优先和全局共享披露预算保持不变。

### 例子与边界

用户问“数据库超时改成了多少”：snapshot 精排分为 0.9，time_span 自身为 0.2，
开启 `rollup=True, hierarchy_role=TIME_SPAN` 后父分变为 0.9；即便父没有索引命中也
可以准入。另一个 snapshot 得 0.7 不会把父分累加成 1.6。默认 depth=0 只返回父，
depth=1 再按结构读取原始证据。

本次不增加第二次独立归一化的“后代召回池”，避免不可比较分数；也不增加 top-M、
子相关性排序、score_propagation 策略或新的建树层级。MultimodalRetriever 的分支
RRF 尚未适配上卷，明确拒绝 rollup=True，不能视作已支持。

上卷只消费既有召回/精排预算内实际物化的候选，不扫描所有 MemoryUnit。
当前 CompositeDomainStore 裸 id 点读按查询精确物理 Scope 读取，不自动扫描 session；
默认“session 内 snapshot、home 内 time_span”布局不能保证在 home 查询时召回到叶。
而 session 限定查询也不能上卷到更宽的 home。跨 session 候选身份/取数与 Fuser 裸 id
归并是已有边界，未在本阶段做存储协议改造；祖先的完整身份隔离不等于解决了该问题。

### 验证

确定性测试覆盖三条存储路径、三种融合器及有/无精排、父未命中准入、MaxP 不累加、
最近角色、直接父、范围预检、过滤/历史状态、坏边/环/上限、同名跨 Scope 父的披露及
展开来源、两种真实 Engine、HTTP/CLI 共用协议和真实跨空间上卷+展开。

完整 unit 回归 **2585 passed、5 skipped、480 deselected**（78.72 秒），比阶段 5
增加 80 个通过项；阶段 6 的 80 项定向用例复查通过。5 个跳过仍为真实 LLM/Redis
用例，5 条 warning 仍为既有未知 asyncio marker；未运行真实外部后端或 LoCoMo 等评测。

17 个变更 Python 文件通过 Ruff（含 --fix 后复查），git diff --check 通过。按已有
CodeCheck 十条规则检查生产、测试、fixture 和邻近定义，修正行长/import、推导式变量
遮蔽，并将更新的 Discloser 多行签名说明移到模块文档；无新增本地预审风险。
原有多参数、邻近签名/docstring、可静态化及变量遮蔽风险未扩范围重构；无业务 assert、测试
protected-access 或无返回值调用赋值。未执行 GitCode 云端 CodeCheck，本地检查不代表
云端通过。不暂存或创建提交，原 tree-mem 分支保持不变。

## 阶段 7 落地（2026-09-10）

本阶段把已有两层 TIME 树扩展为 **snapshot → time_span → scene**，并补齐可选的
父节点语义摘要与 L0/L1。统一 EvolveTaskOptions、HierarchyComposeOptions、SearchOptions
继续使用；不新增公开方法、召回参数、周期任务或自动触发入口。

### 做了什么

- 两层兼容：原 `[TIME_SPAN]` 请求继续可用；新增 `[TIME_SPAN, SCENE]`。
  profile 可声明这两种链，请求可选配置链的短前缀，但不能超出上限。无 profile 时
  使用默认算法。已有两层旧树可显式重建升级；不支持隐式降级已挂 scene 的树。
- scene 分组：消费有序 time_span，系统上下文变化、前段非空结束信号、累计跨度
  超过上限、可选相邻向量余弦低于阈值，任一命中即切；session 变化本身不切 scene。
  默认跨度为滚动 86400 秒，不是自然日；单个超长 time_span 不强拆。scene 的上下文/
  结束信号下传给 TimeSpanMerger，避免底层合并吞掉切点；信号只上提组尾非空值。
- 父正文：默认仍为有界确定性摘录。启用 `summary_mode=llm` 后，time_span 输出
  连续活动摘要，scene 输出目标/行动/结果。模型只改父正文，ID、父子边、区间和
  片段/记录计数由代码生成；snapshot 除父边外全部字段保持不变。
- 先结构后内容增强：可选相似度只消费确定性 time_span 摘录，不含时间表头；
  全树分组结束后才自下向上做 LLM 摘要，再调用显式注入的 LayerAnnotator。
  只向 annotator 提供新父副本，只采纳其合法 L0/L1，不采纳正文和结构修改。
  短正文遵循原阈值，可以没有 L0/L1，不硬凑摘要。
- 显式依赖：Composer 可配置 `embedder`、`llm`、`layer_annotator`；Python 构造
  用 HierarchyModelDependencies 聚合。相似度默认关闭；启用却缺模型/向量非法则失败，
  不替换为占位模型或静默改变切分规则。摘要/标注运行失败保留摘录/空层并记 warning，
  不把非结构增强失败写成 repair；缺必需依赖是装配错误，不是运行期降级。
- 完整重建：窗口命中旧 scene 时，收齐它的全部 time_span 与 snapshot，即使窗口
  只命中第一个片段或落在片段间隙。相交根内部的区间外兄弟也参与，不能只替换局部。
  读取沿完整 Scope+id，scene 的子父层必须驻留 home；不会扩大授权 Scope。
  有旧父时再流式扫描一次反向引用，发现漏列的区间外子或外部父重复占有就拒绝写入。
  叶和父数量都有限额；不缓存全范围节点、不截断候选继续构建。
- 持久化：先分层保存新 scene，再保存新 time_span，最后切换 snapshot 父边；
  完成后归档并清空两层旧父的结构边，移除旧索引、刷新新索引。保持既有部分失败与
  repair 语义，不提供事务、自动回滚或自动修复。

### 例子

上午会话记录“设计锁”，下午另一会话记录“验证锁”：TimeSpanMerger 先按 session
生成两个 time_span；若满足 scene 的配置判据，它们归入同一 scene。启用模型摘要时，
scene 可以描述“目标：实现锁；行动：设计和验证；结果：通过”。未启用模型时是两段
记录的有界摘录，不能把它称为已完成语义理解。

按 `hierarchy_role=SCENE` 查询默认只返回 scene；`expand_depth=1` 可查看 time_span，
`expand_depth=2` 再看 snapshot 原文。`rollup=True` 继续沿用阶段 6 的父级准入和 MaxP；
本阶段通过真实建树后的 time_span→scene 链路验证它，不重新实现召回算法。

### 本阶段不采用的方案与边界

- 不让 LLM 决定树边或切分判据，避免摘要生成波动改动结构；但显式语义 Embedder
  的模型/输入变化仍会影响相似度切分，不能声称所有配置下分组完全不变。
- 不只重建窗口内叶，也不复用残缺旧中间层；选择整棵相交旧子树重建，代价是读取/
  摘要范围可能大于原窗口。有旧父时反向核对还会增加一轮已授权范围扫描。
- 不把摘要/标注失败当作结构失败。日志记录内容增强降级；只保留现有结果 DTO，
  未新增公开摘要质量诊断字段。截断输入可能丢失细节，模型摘要准确性仍待数据集评测。
- 不实现 event、其他 kind、settle、auto derive、周期建树、ensure、top-M、结构生命周期
  自动联动。尚未改变 stage 6 的跨 session 原始召回/裸 id 点读协议及多模态上卷限制。
  home 里的 time_span/scene 可召回、再按完整子引用展开到各 session，不代表 home
  普通召回已经能物化所有 session 内 snapshot。
- 不改变 infer=true/false 的写入分流；只在显式 HIERARCHY 中处理符合条件的 TIME
  snapshot，不按 infer 再分组或筛除。Scope/路由权限限制、可选锁边界保持不变。

### 验证

确定性测试覆盖 scene 时间边界/UTC/原子超长片段、上下文/结束信号、可选相邻余弦、
非法向量和配置、模型摘要格式/输入限额/降级、父标注阈值和字段隔离、三层写入失败、
两层升级、完整旧子树重建/限额/坏边、两种真实 Engine、scene 查询与两级原文展开、
上卷和 HTTP/CLI 共用协议。

最终完整 unit 回归 **2665 passed、5 skipped、480 deselected**（78.13 秒），比阶段 6
增加 80 个通过项；新增 80 项定向复查全部通过。3 项 `python -O` 校验冒烟通过，确认
角色链及相似度参数校验不依赖 assert。5 个跳过仍为真实 LLM/Redis，5 条 warning 仍为
既有未知 asyncio marker；未运行真实模型/外部存储或 LoCoMo、LongMemEval 质量评测。

18 个变更 Python 文件 Ruff 通过，git diff --check 通过。按既有 CodeCheck 十条规则
检查生产、测试、fixture 与邻近定义，修正新增推导式变量遮蔽和格式问题；无新增本地
预审风险。邻近既有的 LayerAnnotator.operator_type 可静态化、test_hierarchy_api.py
4 处推导式变量遮蔽未扩范围修改；抽象契约方法不作为缺少 staticmethod 处理。
无新增业务/辅助失败路径 assert、测试 protected-access 或无返回值调用赋值。
未执行 GitCode 云端 CodeCheck，本地检查不代表云端通过。未暂存或提交，原 tree-mem
分支保持不变。

## 阶段 8 落地（2026-09-10）

本阶段补齐 **snapshot → time_span → scene → event** 四层 TIME 树，保留原两/三层
请求。继续使用 HierarchyComposeOptions、EvolveTaskOptions、SearchOptions；没有新增
公开方法或召回参数，仍只通过显式 HIERARCHY 任务构建。

### 做了什么

- EventBuilder 按 UTC 起点稳定排序，只对相邻 scene 分组。配置的系统上下文变化、
  可选实体重叠低于阈值、可选语义余弦低于阈值，任一命中即切。没有总时长或自然日
  限制，可以跨天；A/B/A 三段不会绕过 B 把两个 A 重组到一起。
- 实体重叠取相邻场景的去重集合交集大小，除以较小集合大小；任一为空时不据此切分。
  阈值范围均为 [0,1] 有限数值，相等不切；相似度默认关闭，启用时必须显式配置
  Embedder，坏向量或调用异常在任何摘要和写入前拒绝，不静默回退分组算法。
- 上下文切点逐层保留：event 的 boundary/carry 系统键下传至 scene、time_span。
  只有显式上下文键会影响下层边界；实体/语义判据只对已形成的 scene 生效，不反向
  重切 scene。scene 和 event 共用正文选择/向量校验逻辑，跳过时间/数量表头。
- event 默认正文是有界场景摘录，默认最多 20 个场景、每个 100 字符；完整 child_ids
  不截断。启用 `summary_mode=llm` 时取 JSON 的 pattern/steps/outcome，生成任务模式/
  步骤/结果。表头时间、场景/片段数量和所有树边始终由代码生成。
- 先固定四层结构，再按 time_span→scene→event 做摘要，最后统一标注父 L0/L1。
  复用既有模型依赖和降级：摘要异常或非法 JSON 保留摘录；标注只采纳新父副本的
  合法 layers。snapshot 除父边外不改正文、原始时间、来源、tier 或生命周期。
- event 的 tier 固定 PROCEDURAL，是本算法的分类约定，不表示已形成经验证的技能。
  event_type/template_id/confidence 等键不从 LLM 自动生成；只接受请求 metadata，
  或显式配置后上提一致子值。用户元数据继续取相等交集，infer/procedural/middle 不传播。
- 完整区间重建：命中旧 event 时收齐 scene→time_span→snapshot 全部后代，区间只落在
  场景间隙或仅覆盖部分后代也一样。所有父驻留 home，叶保留原 Scope；仍按完整 Scope+id
  点读，先验证边界。缺子、非法角色/反向边/区间、漏列入边、分页漂移和超限都在写前失败。
- 父总数上限扩为 3 × max_leaves，各层补齐继续受 max_leaves 保护；不截断候选建树。
  允许两/三层树整体重建升级到 event，不允许以短链隐式降级已有高层。
- 持久化先依次写 event、scene、time_span 本体，再切换 snapshot 父边，随后归档并
  清空旧父边、移除旧索引和刷新新索引。沿用 complete/repair 与部分失败语义，不提供事务。

### 例子与召回

周一“分析锁问题并写修复”，周二“跑回归并验证修复”，可以先分别形成两个 scene。
若 event 的上下文、实体和语义判据未触发切分，它们归到同一个 event；启用 LLM 时，
可以生成“任务模式：问题修复；步骤：分析、实现、验证；结果：通过”的摘要。
这段例子不是固定模型输出，真实摘要准确性仍需评测。

`hierarchy_role=EVENT` 默认只返回 event；`expand_depth=1/2/3` 分别展开到 scene、
time_span、snapshot，仍受共享披露预算及节点数量上限约束。`rollup=True` 复用阶段 6
的父级准入/MaxP，不重新定义分数算法。两/三层和 SCENE 的深度 2 原文展开继续可用。

### 取舍与未实现范围

- 所有 event 分组判据默认关闭时，选定范围内所有 scene 形成一个 event；这只是默认
  结构，不是同一任务的语义证明。业务需要通过 profile 显式配置上下文/实体/语义判据。
- 不采用跨非相邻节点的聚类或多父挂接，保持时间连续与严格树结构。因未设时长上限，
  event 完整重建可能读取远大于请求时间窗口的后代；超限时拒绝，不能只更新局部。
- 不让 LLM 改树边或自动生成系统置信度。摘录和模型输入都有上限，可能遗漏细节；
  event 的 PROCEDURAL 分类或摘要不能替代事实校验，也不保证长期记忆评测提升。
- 不实现 settle、周期/增量维护（留给阶段 9）、auto derive、ensure、top-M、其他 kind
  算法或结构生命周期自动联动。不改变 infer 分流、跨 session 原始召回/裸 id 点读、
  多模态上卷限制和临时事件循环的后台生命周期。

### 验证

定向测试覆盖相邻/跨天分组、实体集合与空实体语义、可选余弦和坏向量、严格参数、
元数据与原文隔离、先结构后摘要/标注及降级、短链兼容与升级/拒绝降级、四层写入顺序
和故障、完整重建与窗口外坏子/限额、两种 Engine 的 event 查询、三级展开和 MaxP 组合。
完整 unit 回归 **2754 passed、5 skipped、480 deselected**（80.88 秒），比阶段 7
新增 89 个通过项；本阶段 89 项定向测试全部通过。6 项 `python -O` 拒绝路径和合法
四层角色链冒烟通过，确认运行校验不依赖 assert。5 个 skip 和 5 条既有 asyncio marker
warning 延续原基线；未运行真实 LLM/外部存储及 LoCoMo、LongMemEval 质量评测。

16 个变更 Python 文件 Ruff 通过，git diff --check 通过。按既有 CodeCheck 十条规则
检查生产、测试、fixture 与相邻定义，修正 2 处新增推导式变量遮蔽及导入/行长问题；
没有新增本地预审风险。原有 LayerAnnotator.operator_type 静态化建议及 test_hierarchy_api.py
4 处推导式变量遮蔽不扩范围修改；抽象契约方法不计为静态化问题。没有新增生产/辅助
assert、测试 protected-access 或无返回调用赋值。未运行云端 CodeCheck，本地通过不等于
云端通过。未暂存或提交，不修改原 tree-mem 分支。

## 背景

现有记忆模型已经覆盖三轴，彼此独立、互不推导：

1. **同 unit 披露轴**：`ContentLayers` 与 `DisclosureLevel` 决定一条
   `MemoryUnit` 以 L0 概要、L1 片段还是 L2 全文进入上下文。L0/L1/L2 只表示
   same-unit compression，不表示节点之间的关系。
2. **多模态构建轴**：多模态构建（F05）对**一条原始媒体源**（首期视频）产出
   CLM/ELM 等多条不同概括粒度的 `MemoryUnit`，用 `system_metadata.memory_level` +
   `provenance` 表达单媒体源构建粒度，不表示跨源的结构包含。
3. **认知抽象轴**：`MemoryTier` 与既有演进模式区分工作记忆、情景、语义、
   程序性、核心与归档等认知角色。

以上三轴仍不能单独回答“一段时间窗口内、跨多条原始源（文本/视频/图片…）的结构
包含与下钻”。因此本特性引入第四轴：

4. **树结构轴**：由 `HierarchyRef` 表达跨 `MemoryUnit` 的包含关系，
   以父节点作为可检索概要，以子节点作为可按需展开的证据。

四轴可在同一节点上共存，且互不推导：

| 轴 | 载体 | 作用域                   |
|---|---|-----------------------|
| 同 unit 披露 | `ContentLayers` | 单 unit                |
| 多模态构建 | CLM/ELM metadata + provenance | 单媒体源 → 多 unit（见 F05）  |
| 认知抽象 | `MemoryTier` | 单 unit 认知角色，unit 可以演进 |
| 树结构 | `HierarchyRef`（本文，首期 TIME） | 跨 unit / 跨源           |

## 架构裁决：树结构轴与其他三轴的边界

树结构轴与既有三轴正交。任意一轴的值都不能推导另外一轴；同一节点可同时携带
四轴信息。

### 与同 unit 披露轴

1. `ContentLayers` / `DisclosureLevel` 只描述**同一** `MemoryUnit` 的压缩披露，
   不表达跨 unit 的父子包含。
2. 父节点与子节点各自可以有独立的 L0/L1/L2；树展开选择的是**哪个节点**进入
   上下文，披露级别选择的是该节点**以何种压缩度**呈现。
3. **禁止**用 L0/L1/L2 或把子节点正文塞进父节点 `layers` 来模拟树结构。

### 与多模态构建轴

1. **禁止**用 `HierarchyRef` 表达同视频内 ELM⊃CLM——只用 F05 的 provenance/metadata。
2. TIME 建树的叶可以是文本直写 unit，也可以是 F05 产出的 CLM/ELM（或其它模态记忆）。
3. **建议叶粒度**：默认以 CLM（及文本叶）作为细粒度权威叶；ELM 可作为并行候选，
   首期**不要**自动把 ELM 写成 TIME 父节点（父由 `HierarchyComposer` 生成）。
4. `evolve(HIERARCHY)` **不**调用、不替代视频理解流水线；缺 F05 时对视频源可降级或跳过。
5. 未来若将单视频 CLM/ELM 升为 `HierarchyKind.MEDIA`，单独立项 RFC，不在首期混用。

### 与认知抽象轴

1. `MemoryTier` 表示认知角色（工作/情景/语义等），不是树位；`HierarchyRef.role`
   （如 snapshot/scene/event）表示结构树位，不等于 tier。
2. 父子节点可各自选择不同 tier；**禁止**用 `MemoryTier` 枚举或 evolve 模式映射
   代替树深、父子边或 `HierarchyKind`。
3. 既有非 `HIERARCHY` 演进模式不暗改 `HierarchyRef`；`evolve(HIERARCHY)` 只维护
   树结构边与派生父。

## 决策（总体设计；当前落地范围以阶段 1–8 小节为准）

下文包含未来目标，不表示各组件、策略和公开入口已经实现；未落地部分不能作为当前
运行时行为依据。公开 HIERARCHY 已交付阶段 8 的显式两/三/四层 TIME，阶段 7 已接入
父摘要与 LayerAnnotator 标注；FORGET 断边、后台自动派生和修复仍为后续目标。
展开已在阶段 5、上卷已在阶段 6 落地。

### 1. 首期采用内嵌 `HierarchyRef`

首期把结构引用内嵌到 `MemoryUnit`，不新增独立边存储。主要原因是：

- KV 中的 `MemoryUnit` 继续作为完整真源，目标索引可从真源重建；
- 父命中后的常用读取是按有序 `child_ids` 点读，首期无需额外 join；
- 缺少 `hierarchy` 的 `_v=4` 数据可以按“非层级节点”兼容读取；
- 可先验证单 kind 严格树、重建与展开语义，再决定是否承担多父图的复杂度。

这是一项首期边界，不是否认独立边存储的长期价值。当同一节点必须在同一种 kind
下拥有多个父节点，或边属性、跨 kind 组合查询成为主路径时，再评估迁移。

公开数据结构、序列化兼容和错误语义已落入
[S07-common.md](../../specs/S07-common.md)；本文只记录选择理由和设计约束。

### 2. `HierarchyRef` 的字段职责

`HierarchyRef` 用 `kind/role` 标识结构维度与树位，用 `parent_id/child_ids` 保存直接且
有序的双向边，用 `span_start/span_end` 表示覆盖区间，并用 `ordinal/status` 表示稳定
顺序与结构修正状态。`status` 是必填字段，默认 `HierarchyStatus.ACTIVE`，取值只允许
`ACTIVE/DISMISSED`；归档、遗忘和版本失效完全由 `LifecycleState`
管理，不进入结构状态。

TIME 节点必须声明有效 span，非 TIME 节点可选；任何已声明的 span 都必须成对、有效且
满足父覆盖直接子。首期仍坚持**同 org+space**、单 kind 严格树、双向一致、无环、稳定
顺序和叶权威；`user`/`agent`/`session` 可按 compose profile 放宽（见决策 2b），默认展开
不隐式跨 kind。具体字段类型、默认值、完整不变量与错误语义见
[S07-common.md](../../specs/S07-common.md)、[S03-control.md](../../specs/S03-control.md)
和 [S04-retrieval.md](../../specs/S04-retrieval.md)。

### 2b. 树结构边的 scope 规则（非五维全等）

父子节点之间的 scope ，不是 `Scope(org, space, user, agent, session)` 五维
全等，会挡住 TIME 的核心场景：跨多个 session 概括、乃至同租户下跨多个 user 概括。
首期的目标契约是：

| 维度 | 规则                                                        |
|---|-----------------------------------------------------------|
| `org` + `space` | **硬边界**：父子必须相同；跨 space / 跨 org 的树边一律拒绝。（与 F03 租户隔离一致） |
| `session` | **默认可跨**：同 user（或 profile 允许的主体）下连续多 session 可挂同一 TIME 树  |
| `user` / `agent` | **默认不可跨**；同 org+space 跨 user/agent 建树须 compose profile 显式开启 |

父节点通常写在 build 请求的 **tree home scope**（例如清空 `session` 的用户级归属，或
策略开启时的 space 级归属）；权威叶可仍驻留在更细的 session scope。因 id 只在完整
Scope 内唯一，跨细粒度 scope 的边必须携带 `child_scopes` / `parent_scope`（见 S07），
缺省时仍表示与持有边的 unit 完整 Scope 相同。

跨 space 的「共享记忆」继续走 F03 的 grant / shared space，**不允许**用 `HierarchyRef` 穿越
租户硬边界。

### 3. 血缘、版本与结构三分

三种引用表达不同事实，必须分离：

| 载体 | 回答的问题 | 生命周期 |
|---|---|---|
| `provenance` | 这条记忆由哪些记忆抽取、升华或合成而来？ | 随演进与可追溯性管理 |
| `supersedes` | 这个版本取代了哪个旧版本？ | 随版本链和 valid-time 管理 |
| `HierarchyRef` | 这个节点结构上包含谁、隶属于谁？ | 随建树、剪枝、重建与展开管理 |

`evolve(HIERARCHY)` 可以作为建树调度入口，但其产物关系仍只写
`HierarchyRef`。建树不意味着生成 `provenance`，结构重建不意味着
`supersedes`，沿血缘追溯也不承担树展开。

### 4. 丰富实体复用 `MemoryUnit` 槽位

不同角色不新增各自的实体表。每个节点仍是一条完整 `MemoryUnit`，领域信息按语义
进入既有或新增槽位：

| 信息               | 槽位 |
|------------------|---|
| 节点身份             | `MemoryUnit.id` |
| 正文、叙述            | `segments` 及其 `content` 合并视图 |
| 同 unit 压缩表示      | `ContentLayers.l0/l1`；L2 仍是 `MemoryUnit.content` |
| 认知角色             | `MemoryUnit.tier` |
| 结构身份、父子边、区间、顺序、状态 | `HierarchyRef` |
| 叶事件时间和双时间语义      | `MemoryUnit.temporal` |
| 设备、应用、标题、路径、模板、置信度、价值分等领域字段 | `system_metadata` 或 `user_metadata`，按系统解释/用户透传职责区分 |
| 主题分类             | `tags` |
| 原模态证据            | `segments[].assets` |
| 抽取或合成来源          | `provenance`，仅用于真实演进血缘 |
| 版本替换             | `supersedes` |

这使丰富角色可以共享存储、索引、生命周期和披露能力，又不把领域字段提升为所有
kind 都必须理解的核心类型。

首期推荐统一使用小写 snake_case metadata 键，TIME 叶可使用 `device_id`、`app`、
`window_title`，`event` 父可使用 `event_type`、`template_id`、`confidence`，
DIRECTORY 节点可使用 `path`。这些键是领域投影，不是 `MemoryUnit` 一级字段；
construction 可以提供 pack/unpack 辅助，但不得让 common 类型依赖某一种 kind。

### 5. `HierarchyRole` 与 `MemoryTier` 只提供指导映射

role 表示树位，tier 表示认知角色，两者不做硬编码等价。默认建议如下：

| role | 建议 tier | 理由                               |
|---|---|----------------------------------|
| `snapshot` | `EPISODIC` | 权威事件叶                            |
| `time_span` | `EPISODIC` | 连续活动片段                           |
| `scene` | `SEMANTIC` | 场景回顾仍以情节摘要或核心脉络为主                |
| `event` | `PROCEDURAL` | 表达任务流程或可复用模式                     |
| `profile` | `CORE` | 稳定画像；**独立 MemoryUnit**，不进入 TIME 主树，也**不**用 `parent_id`/`child_ids` 与 TIME 节点互挂 |
| `root` | `SEMANTIC` 或 `CORE` | 结构入口                             |
| `node` | 由内容决定 | 通用 kind 不预设认知角色                  |

构建器可以按领域策略覆盖建议值，但不得用 tier 代替 role。

### 6. 写叶与构建父节点分离

普通 `write` 继续负责写入权威叶或调用方明确提供的单节点，不同步构建整棵树。
叶可以没有 `HierarchyRef`，也可以显式标记为某个 kind 的叶角色。

父节点及父子边由显式或后台的 `evolve(HIERARCHY)` 构建。构建过程读取目标范围内
的权威叶，生成父节点正文与可选 `ContentLayers`，写入有序子引用，并回写子节点
的直接父引用。默认写路径保持轻量，也让父节点能够按区间重新推导。

公开的 write/evolve 参数、调度与返回结构分别由
[S02-memory-api.md](../../specs/S02-memory-api.md)、
[S03-control.md](../../specs/S03-control.md) 和
[S05-construction.md](../../specs/S05-construction.md) 定义；本文不复制目标签名。

### 7. 演进模式不隐式混写 hierarchy

普通 `write` 与 `EXTRACT/ASSOCIATE/CONSOLIDATE` 不因产生 unit、血缘或图关系而自动
挂树；`HIERARCHY` 才负责创建或重建父节点和双向直接边。`FORGET` 必须同时断开遗忘
节点的直接父边和全部直接子边：从父 `child_ids` 移除该节点、清空该节点
`parent_id`，并清空其 `child_ids` 及所有直接子的对应 `parent_id`；这些节点保留且
不发生级联删除。逐模式字段行为和删除顺序以
[S05-construction.md](../../specs/S05-construction.md) 与
[S03-control.md](../../specs/S03-control.md) 为准。

### 8. `replace_in_span` 以叶权威为边界

TIME 父节点会因切分策略、修正或新增叶而重算。`replace_in_span` 只替换与目标区间
相交的派生父层及其索引，完整断旧边并一致挂新边，所有权威叶及其内容保持不变。
区间边界不得留下半断开的双向引用。存储仍提供通用 CRUD，具体替换步骤、失败修复与
事务边界由
[S05-construction.md](../../specs/S05-construction.md)、
[S03-control.md](../../specs/S03-control.md) 与
[S06-storage.md](../../specs/S06-storage.md)。

替换区间若切过一个旧父节点中部，构建器只能扩大替换集至该旧父的完整覆盖范围，
或者在任何写入前拒绝请求；不得保留“半个旧父”。断开旧边后尚未重挂的子节点只清空
`parent_id` 成为未挂接节点，仍是可检索、可再次建树的权威节点，不进入 FORGOTTEN，
也不因空父回收而被删除。

### 9. 检索支持按父侧 role 优先召回，再按需展开

层级检索分成两个阶段：

1. **父层召回**：调用方按 kind、父侧 role、区间等结构条件筛选父节点，走现有混合
   召回、融合、重排和阈值链路。`expand_depth=0` 只返回直接命中的父节点，不自动附带
   子全文；省略 role 时同 kind 下所有活动角色均可参与，不再称为“只召回父节点”。
2. **子树展开**：调用方或检索编排依据深度与预算，沿父节点有序 `child_ids` 点读
   子节点；展开默认不跨 kind。

父优先使粗粒度摘要成为稳定入口，同时保留“先看概要、再取证据”的交互方式。
阶段 6 已开放 rollup，允许实际命中的后代准入父并传播 MaxP，具体边界见阶段 6 小节。
检索轨迹必须区分父层命中与子树展开阶段，并记录根节点、展开深度、返回节点数和预算
截断原因，使父→子的证据路径可审计。

`RetrievedItem` 保持扁平，不嵌套 `child_ids` 或树容器。调用方在同一次 `search` 中通过
非零 `expand_depth` 展开；**不另设公开 `MemoryAPI.expand`**。`rollup` 把后代相关性
传播到目标父角色，默认不展开后代；“父命中”“分数上卷”和“内容展开”是三个可独立启用的动作。

层级过滤、展开和结果结构的精确公开契约已写入
[S02-memory-api.md](../../specs/S02-memory-api.md) 与
[S04-retrieval.md](../../specs/S04-retrieval.md)。

### 10. 展开选子与既有 `max_tokens` 共用，不另设树预算池

父子结构新增两类跨节点决策：

- **分数传播（阶段 6）**：已采用 MaxP，把父自身得分与命中后代最高分合并；
  top-M 或子相关性阈值收敛、其他传播算法仍留待后续基准验证，不属于本次交付。
- **节点准入与主披露级**：`expand_depth>0` 时，选哪些子节点及每个节点的主
  `DisclosureLevel`，与父命中一起消耗既有 `RetrievalQuery.max_tokens`（来自
  `context.extensions["max_tokens"]`），**不**另设 `expand_budget_tokens` 或独立
  `tree_budget` 控制面。

现有 Discloser 的职责仍是对**单个 unit**选择或塑形 L0/L1/L2 内容；跨节点遍历由
Retriever 内的 Expander 完成；选子回调调用 Discloser 塑形后，据实际主字段分配
共享预算。二者分工不同，不在塑形前用原始正文估算所有披露层级。
由于 `RetrievedItem` 始终返回 abstract/overview/content 全字段，实际响应可超过该逻辑
预算；严格 wire-size 投影不在当前契约内。

### 11. TIME 是结构 kind，不是时间字段或召回通道

`HierarchyKind.TIME` 用树结构组织时间维度的多粒度记忆，典型角色顺序是：

```text
event（可选森林根）
  └─ scene
       └─ time_span
            └─ snapshot
```

TIME 的主要约束是：

- `snapshot` 通常是权威叶，事件时刻使用 `MemoryUnit.temporal.t_event`；
- 区间父节点使用 `HierarchyRef.span_start/span_end` 表示覆盖范围；
- 直接子节点按时间稳定排序；
- `profile` 属于画像组织：产出独立 `MemoryUnit`（常 `CORE`），**不**通过 TIME 的
  `parent_id`/`child_ids` 与 snapshot/time_span/scene/event 互相关联；画像检索走
  既有召回/标签/tier，不以 TIME 结构边表达；
- 高层可重建，叶不可因父层重建被清除。

`MemoryUnit.temporal` 是双时间字段，`RecallChannel.TEMPORAL` 是检索中的时间过滤
通道，二者都不等于 `HierarchyKind.TIME`。TIME 负责“谁在时间结构上包含谁”，
时间字段负责“何时发生、摄入、生效或失效”，通道负责“如何按时间约束召回”。

### 12. 多 kind 复用协议，避免新增结构轴

同一套父子协议还可表达：

- `HierarchyKind.DIRECTORY`：`root`/`node` 组成路径浏览树；
- `HierarchyKind.TOPIC`：主题根与主题节点组织相关记忆；
- `HierarchyKind.CLUSTER`：聚类父节点包含成员节点；
- `HierarchyKind.CUSTOM`：由调用方或插件定义的包含结构。

每种 kind 可以拥有自己的构建策略和排序规则，但共享树校验、父优先召回、展开、
分数传播与预算机制。首期默认单 kind 遍历；同一节点的多 kind、多父或图关系不做
隐式合并，非包含关系继续由 GraphStore 表达。

### 13. 模块分解与实现顺序

树结构横跨七个内核模块，但每层只承担一种职责：

| 模块 | 本特性职责 | 不承担的职责 |
|---|---|---|
| `common` | 公共枚举、`HierarchyRef`、codec、无副作用树校验 | 建树和存储事务 |
| `storage` | KV 真源、索引 metadata、scope 隔离 CRUD | 解释父子业务语义或级联 |
| `construction` | `HierarchyComposer`（构建算子，由 Evolver/`evolve(HIERARCHY)` 调用）、kind pipeline、父内容生成、索引更新 | 鉴权和召回 |
| `retrieval` | 结构过滤、Retriever 内 Expander、MaxP、与 `max_tokens` 共用的展开准入、轨迹 | 建树和修复 |
| `control` | 策略闸门、任务调度、结构事务（落在既有治理/Engine）、ensure、生命周期联动 | kind 专属切分算法 |
| `api` | 参数装配、PEP、错误透传 | 数据面编排 |
| `ingest` | 把可信来源提示映射为无边叶身份 | 建父、查父或回写边 |

实现依赖顺序固定为：

```text
common → storage → construction → retrieval → control → api
                      ↑                         ↑
                    ingest --------------------┘
```

这里表示类型和能力依赖，不表示所有代码必须串行开发。construction 不得反向依赖
control 或 Scheduler；control 负责提交任务，construction 只执行构建请求。
ingest 与 api 可以在公共契约稳定后并行实现。jiuwen_memory_entry 和 jiuwen_memory_adapter 只做薄适配，
不承载内核建树算法。

结构事务的业务编排归 control：它负责 scope/kind/span 并发闸门、任务终态，以及
update/delete/FORGET/SUPERSEDE 路径。construction 的 `HierarchyComposer` 负责生成并
校验候选子树，并通过不含鉴权和 Policy 的提交端口完成 KV/索引写入。control 调度
evolve/replace 并以 `HierarchyComposeResult.complete` 判断终态；Composer 不读取运行时
Policy，也不自行提交后台任务。

### 14. 构建层采用统一 Composer 加 kind pipeline

`HierarchyComposer` 与 `Extractor`/`Abstractor` 等一样，是 `ConstructionOperator`
实现：由控制层通过 `evolve(scope, EvolveTaskOptions(mode=HIERARCHY, ...))` → Evolver 调度调用，不自行鉴权、
不自行提交后台任务。它是跨 kind 的统一构建入口，负责请求校验、pipeline 选择、
结构校验、持久化和修复报告；kind 专属算法由可替换 pipeline 承担：

```text
HierarchyComposer
├─ TimeHierarchyPipeline
│  ├─ TimeSpanMerger
│  ├─ SceneSegmenter
│  └─ EventBuilder
├─ TopicHierarchyPipeline（后置）
├─ DirectoryHierarchyPipeline（后置）
└─ HierarchyMaintainer
```

每个 stage 接收同 kind、稳定排序、且满足决策 2b scope 规则的叶或中间节点集合，以及
构建 span 和不可变构建选项；输出候选父 `MemoryUnit` 与待应用的直接边变更。stage 不直接
鉴权、调度或提交存储事务，因而可以用内存输入做确定性单测。`HierarchyComposer` 在所有
stage 完成后统一验证整棵候选子树，再决定提交或返回错误。

`EvolveMode.HIERARCHY` 直接委托 `HierarchyComposer`，不进入 EXTRACT/CONSOLIDATE 的
Dedup 主路径。父摘要的内容去重可以作为以后独立策略加入，但不得让相似性判定改变
树的单父、区间覆盖和稳定顺序。

`HierarchyMaintainer` 处理 dismiss、剪边、空父回收和显式修复。节点正文或 metadata
修改仍走既有 update，不新增“重命名”旁路。Maintainer 可以复用 Composer 的校验与
提交器，但不重新执行内容派生算法；空父默认保留，只有明确策略才能退役，且永不级联
删除权威叶。

精确的请求、结果和算子签名由
[S05-construction.md](../../specs/S05-construction.md) 单点定义，本文只确定组件边界。

### 15. TIME 派生链及各 stage 逻辑

TIME pipeline 的输入是指定 span 内、`role=snapshot`、生命周期和结构状态均可用的权威叶。
输入先按 `span_start`、`temporal.t_event`、请求中的稳定顺序排序；完整 Scope+id 重复、跨 org/space、
跨 kind 或区间非法在进入 stage 前拒绝。profile 未允许的跨 user/agent 同样拒绝。

```text
snapshot → TimeSpanMerger → time_span
      → SceneSegmenter → scene
      → EventBuilder → event
```

| stage | 输入 | 边界判定 | 输出内容 |
|---|---|---|---|
| `TimeSpanMerger` | 连续 snapshot | 会话硬边界、配置的系统上下文键变化、相邻 span 间隔超过阈值时切断；设备仅在配置其上下文键后参与 | 一个连续活动片段，子为 snapshots；阶段 2 正文仅为有界摘录 |
| `SceneSegmenter` | 有序 time_spans | 明确上下文切换为硬边界；主题/任务相似度、最大持续时间和显式结束信号形成软边界 | 一个可回顾场景，子为 time_spans |
| `EventBuilder` | 有序 scenes | 按任务目标、动作序列和实体重合聚合；不得为了相似度打乱时间顺序或让 scene 多父 | 一个任务流程或可复用模式，子为 scenes |

硬边界优先于任何语义相似度；软边界的阈值和特征组合属于 compose profile，不写死在
公共类型。算法必须确定性消费已排序输入：同样的输入、profile 和模型版本应产生相同
分段顺序。LLM 可用于命名和摘要，但不能绕过硬边界或直接提交结构边。

各层字段生成遵循以下规则：

| role | span | content / layers | tier |
|---|---|---|---|
| `snapshot` | 事件点可表示为起止相同 | 保留权威内容；不由 pipeline 改写 | 通常 `EPISODIC` |
| `time_span` | 直接 snapshot 区间的最小包络 | 连续活动摘要，保留关键应用/标题等 metadata | 通常 `EPISODIC` |
| `scene` | 直接 time_span 区间的最小包络 | 目标、关键动作、结果和证据摘要 | 通常 `EPISODIC`，稳定抽象后可为 `SEMANTIC` |
| `event` | 直接 scene 区间的最小包络 | 任务模式、步骤和结果；可写 `event` 领域 metadata | 通常 `PROCEDURAL` |

父 span 默认取直接子 span 的最小起点和最大终点，不得缩小到遗漏直接子。父正文先由
stage 生成 segments，再由 `LayerAnnotator` best-effort 生成 `layers.l0/l1`。子
`parent_id` 回写时不改子内容、tier、temporal、provenance 或 lifecycle。

`profile` 不进入 TIME 主链，也不得把 snapshot/time_span/scene/event 挂为 `profile` 的
结构子节点。稳定画像应作为 `MemoryTier.CORE` 的独立 unit，或进入 TOPIC 结构；它与
TIME 证据只通过 metadata 或真实演进来源弱连接。把 profile 设为 TIME 根或 TIME 父会把
无界、持续更新的画像强行变成一个时间区间父，破坏 span 和局部重建语义。

首个可交付构建切片 P1 只要求 snapshot→time_span；scene 在 P2 加入，event 在 P3
加入；pipeline 协议从一开始允许缺省后续 stage。

### 16. 字段填充、校验与持久化顺序

Composer 创建父节点时按下列顺序处理：

```text
1. stage 生成候选父的 id、scope、role、span、segments、tier 和领域 metadata
2. LayerAnnotator best-effort 生成 l0/l1；失败保留空 layers
3. 组装候选 parent_id/child_ids 和对子节点的边变更
4. 对完整候选子树校验 scope、kind、单父、无环、排序和 span 覆盖
5. 写入新父 KV，并在同一结构提交中回写子 parent_id/旧父 child_ids
6. KV 成功后 build/update 内容层索引及 hierarchy metadata
7. 返回 created/updated/replaced/repair_required/complete
```

父节点 id 必须新生成；结构派生不写 `provenance`，除非该父正文确实通过既有演进算子
由来源 unit 合成，且这条血缘在脱离层级关系后仍然成立。`metadata` 只接收该 kind
约定的领域键（见决策 4），不得覆盖 id、scope、temporal、lifecycle 或 hierarchy。

`replace_in_span` 在步骤 1 前先读取所有相交旧派生父并扩大替换边界，然后计算“旧边
断开、旧父退役、新父写入、新边挂接”的完整变更集。只有新树整体可验证时才开始写。
索引始终后于 KV；索引失败不会把索引提升为真源，但操作必须返回不完整状态并进入修复。

支持事务的 KV 后端应原子提交全部受影响 unit。不支持事务的后端采用可恢复顺序：
先持久化无活动边的新父，再按稳定顺序切换子边，最后退役旧父；任何中断返回
`complete=false` 和 `repair_required`，任务不得标记成功。修复以 KV 中可见 unit
重新计算双向边和索引，不从旧索引反推真源。

### 17. 运行时 Policy 与不可变 compose profile 分离

运行时 Policy 控制“是否执行”，compose profile 决定“如何构建”：

| 分类 | 内容 | 变更语义 |
|---|---|---|
| 运行时 Policy | 总开关、auto derive、ensure、MaxP、内部/接入形态默认展开深度、top-M | 可以治理时调整；不回写已有树 |
| compose profile | kind、leaf role、parent role 序列、stage 启用、硬边界键、阈值、模型/提示版本 | 装配期固定；变更后通过显式 rebuild 生效 |

首期所有运行时能力默认关闭：普通 add 和 search 行为不变。`auto_derive` 只在叶成功
写入且 profile 能确定有界 span 时提交 BACKGROUND 任务，不阻塞 hot path。
`ensure_on_recall` 只服务显式 kind+有界 span 的召回，并阻塞等待构建终态，避免调用方
请求“确保后召回”却拿到静默的无结构结果。

compose profile 至少定义 `leaf_role`、从近叶到远叶的 `parent_roles` 和每个 stage 的
算法配置。role 序列不放入可随时修改的 PolicyManager，避免运行中改变树形导致同一
scope 出现两套半成品结构。profile 缺失时 ensure 抛 `PolicyError`，auto derive 记录
跳过原因；两者都不得猜测默认 role 序列。

`hierarchy.expand_default_depth` 只供未显式给 depth 的内部或接入形态使用；公开
recall 的默认值始终是 `expand_depth=0`，Policy 不得隐式改写该公开默认。

Policy 键、默认值和校验由
[S03-control.md](../../specs/S03-control.md) 定义；构建请求字段由
[S05-construction.md](../../specs/S05-construction.md) 定义。

### 18. 失败、降级与并发决策

| 场景 | 决策 |
|---|---|
| 父摘要或 layers 生成失败 | 保留结构候选，父 content 使用确定性规则摘要或最低可用拼接，layers 为空；记录诊断 |
| HIERARCHY 部分写入失败 | `complete=false` 并返回逐项 `repair_required`；任务状态不得为 SUCCEEDED |
| `replace_in_span` 中断 | 不删除权威叶；根据 KV 重算未完成边，修复前不宣称替换完成 |
| expand 遇到缺子、跨 kind、环或不可见节点 | 跳过该分支、记录 issue、`complete=false`；不让一个坏分支使所有有效结果失败 |
| ensure 任务失败、取消或超时 | recall 抛 `BackendError`，不降级为普通无层级召回 |
| auto derive 提交失败 | 不回滚已成功写入的叶；记录任务和审计错误 |

同一 `scope + kind` 下存在重叠 span 的 build、replace、update、FORGET 或 PURGE 必须
串行化，或者由后端乐观版本条件检测冲突。并发 write 可以先完成叶写入；若其 span 与
正在替换区间相交，当前 replace 不能悄悄吸收未参与初始快照的叶，必须冲突重试或由
后续增量任务补建。这样保证一次构建的输入快照和结果可解释。

`HierarchyRepair` 只报告结构差异，不借用 provenance trace。修复器重读当前 KV、
重建期望双向边并重建派生索引；无法确定唯一父时停止并返回冲突，不凭 id 顺序猜测。

### 19. 分阶段落地与兼容边界

| 阶段 | 交付范围 | 进入下一阶段的条件 |
|---|---|---|
| P0 | 公共类型、codec、纯函数校验、索引 metadata | 旧数据兼容；环、跨 org/space、非法跨 user/agent、重复子和单 kind 多父被拒绝 |
| P1 | snapshot→time_span、结构提交器、`replace_in_span` | 可重复重建且叶内容零变化 |
| P2 | snapshot→time_span→scene、父侧召回、`search(..., expand_depth=1)`、MaxP | 默认 depth=0 不返回子全文；与 `max_tokens` 共用预算，轨迹可区分展开阶段 |
| P3 | event、ensure/auto derive、修复任务 | 失败状态和后台任务可观测 |
| P4 | 至少一种非 TIME kind | 复用同一校验、存储与展开协议 |
| P5 | Maintainer 修正流（dismiss/剪边/空父回收/修复）和性能优化 | 并发冲突与展开性能达到已设基线 |

兼容边界：没有 `hierarchy` 的 `_v=4` 数据无需迁移即可读取，`_v<4` 仍必须先迁移。
阶段 1 尚无 hierarchy Policy 闸门；无层级提示时保持既有 add/evolve/search 行为。
后续引入开关后，关闭时应保持普通请求结果和错误语义；目标接口未启用时不改变既有
插件装配和 Store 抽象。

## ingest 接入层 （层级叶提示）

本节叶提示接入已在阶段 1 实现。Source adapter 可以在
`RawPayload.system_metadata` 中提供以下保留键：

| 键 | 类型 | 语义 |
|---|---|---|
| `hierarchy_kind` | str | `time` / `topic` / `directory` / `cluster` / `custom` |
| `hierarchy_role` | str | 接入允许的叶角色：TIME 为 `snapshot`，其他 kind 为 `node`；完整枚举见 S07 |
| `hierarchy_span_start` | ISO 8601 str | 可选覆盖区间起点 |
| `hierarchy_span_end` | ISO 8601 str | 可选覆盖区间终点 |

Ingestor 只允许把一组完整且有效的提示映射到当前 unit 的叶安全字段：
`kind`、`role`、`span_start`、`span_end`。映射后的 `parent_id` 必须为空，
`child_ids` 必须为空，`status` 使用 `ACTIVE`。未提供任何保留键时，
`hierarchy` 保持默认空结构。

校验是确定性的：

1. kind/role 必须同时提供；区间必须同时提供或同时缺省。
2. 枚举值必须精确匹配，时间必须可按 ISO 8601 解析，且起点不得晚于终点。
3. `HierarchyKind.TIME` 必须提供区间；其他 kind 可省略区间。
4. 接入提示只接受叶角色：TIME 只接受 `snapshot`；DIRECTORY、TOPIC、CLUSTER、CUSTOM
   只接受 `node`。`time_span`、`scene`、`event`、`profile`、`root` 等父侧
   角色必须由构建层创建。
5. `hierarchy_parent_id`、`hierarchy_child_ids` 或其他试图建立边的保留前缀键一律以
   `ValidationError` 拒绝，不作为普通 metadata 静默保留。
6. 任一叶提示无效时拒绝该 payload 的转换，不产出半有效 `HierarchyRef`；非
   `hierarchy_` 前缀的系统 metadata 继续原样透传；用户命名空间全部原样透传。

消费后的四个提示键从产出 unit 的 `system_metadata` 移除，输入 payload 不被修改；
CloudEngine 不再回注已消费提示。解析路径自行检查字段，不调用公共 `validate_ref` /
`validate_tree`。这些提示只声明当前 unit 的结构身份，不证明边存在；父子边由阶段 2
的独立 Composer 构建，阶段 3 的公开任务在收齐输入后调用它。写入路径本身不触发
建树，后续维护仍未接入。

## 关键数据流（显式两/三/四层建树与查询展开已落地）

树结构专用路径不写入 architecture §14（该节只保留通用 write/recall/evolve 骨架）；
建树与按需展开细节如下。

**建树路径（`EvolveMode.HIERARCHY`）**

```text
evolve(scope, EvolveTaskOptions(mode=HIERARCHY, hierarchy_options=...), security=...)
  → API 校验、WRITE+UPDATE 与空间 UPDATE 判权、hierarchy.enabled 闸门
  → CommandService → Engine 注入同源 Evolver/KV → 专用 HierarchyJob → Scheduler
  → Job 可选获取 home+kind 锁 → 完整分页 → 相交旧根全部父层与叶补齐 → 反向引用核对
  → Evolver(EvolveRequest) → HierarchyComposer 校验与生成 snapshot→time_span→[scene→[event]]
  → 结构固定后自下向上增强父正文 → 可选父 L0/L1 标注
  → IndexBuilder 依次写父、更新子边、归档清理旧父、刷新索引
  → HierarchyComposeResult → JobInfo 的终态、计数、complete 与 repair
```

普通 add 不建树。LayerAnnotator 父标注已实现，目标中的 `hierarchy.auto_derive` 写后
派生仍未实现；不能从上面的显式任务入口推导自动后台触发已经可用。

**读取展开路径（仍是 `search`，无公开 `expand`）**

```text
search(query, context, SearchOptions(hierarchy_kind=..., hierarchy_role=..., expand_depth=N))
  → 既有 QueryParser → 多路召回 → Fuser → Reranker → [rollup] 祖先准入/MaxP → 阈值 → top_k
  → Discloser 塑形根；[若 N>0] 内部准备根与来源
  → 单/跨空间最终选根 → 共用 max_tokens 准入根
  → [若 N>0] Expander 按根顺序 BFS：真源复核 → Discloser 塑形子 → 预算准入
  → RetrievalResult（扁平 RetrievedItem + 可选轨迹 + errors）
```

expand_depth=0 不准备或展开；rollup 已开放，可准入祖先并传播分数，但不自动展开。

## 拒绝的方案

### 1. 用 `ContentLayers` 表示父子节点

拒绝。L0/L1/L2 是同一条 unit 的压缩表示，没有独立身份、生命周期或子证据集合。
把结构角色映射为披露级别会破坏 F01 已确立的 same-unit compression 语义。

### 2. 用 `MemoryTier` 表示 `snapshot`、`scene` 等树位

拒绝。tier 表示认知角色，同一个 role 可以因内容不同选择不同 tier；父子节点的 tier
也可以不同。绑定两者会使目录、主题和聚类结构无法复用。

### 3. 扩展 `provenance` 承载父子关系

拒绝。演进来源与结构包含有不同的遍历方向、重建时机和治理语义。混用后，血缘追溯、
版本治理、删除与展开都无法判断边的真实含义。

### 4. 首期直接采用独立边存储

拒绝作为首期默认。它能更自然地支持多父、多 kind 共节点和丰富边属性，但会增加
新 Store、双写一致性与查询 join。在严格树 MVP 尚未验证前，这些成本没有足够收益。

### 5. 每个角色建立专用实体和存储

拒绝。专用表会复制 scope、生命周期、索引、披露和序列化能力，并把通用层级协议
绑定到单一领域。丰富字段应优先复用 `MemoryUnit` 的结构化槽位。

### 6. 普通 add 同步自动建完整父树

拒绝作为默认。建树可能涉及区间读取、切分、聚类、摘要和多次写入，会扩大 hot path
时延，也使局部写入与全局重算耦合。显式或后台 `evolve(HIERARCHY)` 更符合父可重建、
叶权威的边界。

### 7. 召回父节点时自动返回整棵子树

拒绝。无界展开会放大延迟与 token 消耗，也让调用方无法先看概要再决定是否取证。
调用方显式按父侧 role 召回时，默认不展开；深度和预算必须显式控制。

### 8. 直接扩展现有 Discloser 负责整棵树预算

拒绝。Discloser 已有清晰的单 unit 披露职责。树遍历、节点选择与跨节点预算是独立
问题，由独立编排负责；准入预算以 Discloser 渲染出的实际主字段为依据。

## 验证

阶段 1 的模型、接入与索引投影已落地，完整 unit 回归 1891 passed、5 skipped；阶段 2
补齐内部最小 TIME 构建及受限替换，完整 unit 回归 2082 passed、5 skipped。
阶段 3 增加显式任务入口与完整候选读取，完整 unit 回归 2229 passed、5 skipped。
阶段 4 增加 SearchOptions、直接结构查询与真源复核；阶段 5 交付只读展开与共享预算，
阶段 6 交付父级准入与 MaxP；阶段 7 交付 scene、父内容增强与完整三层重建，
阶段 8 交付 event 四层构建/重建；不等于后续 top-M 或自动演进已实现。
不得用已有 pytest 结果替代未交付能力的实现验证。

以下 P0–P5 是决策 19 的原设计验收分组，不是本次按能力整理的提交阶段顺序。
阶段 1 覆盖模型、叶提示与索引投影，阶段 2 覆盖 P1 的最小内部构建切片，阶段 3
覆盖显式调用、完整取数与任务状态闭环；阶段 4 覆盖直接层级查询，阶段 5 覆盖展开与预算，
阶段 6 覆盖父级准入与 MaxP，阶段 7/8 覆盖三/四层构建、重建及父内容增强；其余验收保留为后续目标，按实际交付
逐项验证，不因属于同一个原设计分组而提前标记完成。

### 阶段 0：设计验收

- [x] 四轴术语在 features、specs 与总体设计中一致，无披露级、多模态粒度、tier、时间字段和结构 kind 混用。
- [x] `HierarchyRef` 字段、兼容读取、错误语义和公开契约进入对应 specs。
- [x] 明确首期单 kind 严格树边界，以及迁移到独立边存储的触发条件。
- [ ] 完成设计评审。

### 阶段 1：模型、叶提示与索引投影（已实现并验证）

- [x] `_v=4` 数据缺少 hierarchy 时读为空结构，vectors/双 metadata/瞬态键行为保持兼容。
- [x] 纯校验拒绝传入集合内的非法 Scope、重复子、环和多父，不宣称核对全库引用。
- [x] 跨 session 及显式允许的跨 user/agent 引用保留完整 Scope，codec 不改变列表顺序。
- [x] 叶提示只来自系统命名空间，消费后不回注，不生成父节点或改变 infer 分流。
- [x] build/update 正确生成六键并清理旧投影；全量 rebuild 恢复不作为已实现能力。

### 后续验收组 P1：构建与重建（阶段 2 部分实现并验证）

- [x] 普通 add 不自动构建父树，显式叶写入仍可工作。
- [x] 内部 EvolveRequest/HIERARCHY 能建立最小父子树；公开任务入口另行验收。
- [x] 对显式备齐的输入，`replace_in_span` 替换相交旧根的完整派生子树，不删除权威叶。
- [x] 父节点内容层在落盘和建索引前按 best-effort 策略生成或安全降级（阶段 7）。

### 后续验收组 P2：构建、检索与预算（阶段 8 部分实现）

- [x] snapshot→time_span→scene→event 可构建、可重复重建，权威叶内容零变化（阶段 7/8）。
- [x] 默认父层召回不自动包含子全文。
- [x] 展开按顺序、深度、kind 与 scope 约束返回子树切片。
- [x] 检索轨迹分别记录父层命中与展开阶段，并能解释展开深度和预算截断。
- [x] MaxP 上卷与祖先准入有确定性测试（阶段 6）。
- [ ] top-M 选子收敛策略有确定性测试。
- [x] 根与后代共享预算，根据 Discloser 实际字段准入节点并确定主 level。
- [x] `MemoryUnit.temporal`、`RecallChannel.TEMPORAL` 和 `HierarchyKind.TIME` 的过滤行为互不替代。

### 后续验收组 P3：调度、策略与修复（任务终态已实现，其余未完成）

- [ ] ensure 阻塞等待任务终态；失败、取消或超时抛 `BackendError`，不静默降级。
- [ ] auto derive 不阻塞 write，提交失败不回滚已成功写入的叶。
- [x] `complete=false` 或存在 `repair_required` 时任务为 FAILED，修复项可观测（阶段 3）。
- [ ] compose profile 变更只通过显式重建生效，不产生两套半成品 role 序列。

### 后续验收组 P4：多 kind 与回归（未完成）

- [ ] 至少一种非 TIME kind 复用相同树校验与展开协议。
- [ ] hierarchy 关闭或字段为空时，既有 write/evolve/recall 行为保持兼容。
- [ ] 完成相关单元、集成、序列化兼容、索引重建与性能基线测试。

### 后续验收组 P5：修正流与性能（未实现）

- [ ] dismiss、剪边、空父回收和显式修复不级联删除权威叶。
- [ ] 重叠 span 并发冲突和展开性能达到已设基线。
- [ ] `profile` 不进入 TIME 主树，也不把 TIME 节点挂为结构子。

### 实现测试矩阵

| 测试层 | 必测内容 |
|---|---|
| common 单测 | codec 缺字段/未知字段；空结构；无环、单父、双向一致、span 覆盖 |
| construction 单测 | 各 TIME stage 的确定性边界；LayerAnnotator 失败降级；replace 不触叶；repair 路径 |
| control 单测 | attach/detach/SUPERSEDE/FORGET 的结构事务；ensure 终态；Policy 关闭 |
| retrieval 单测 | depth=0；稳定展开顺序；MaxP/top-M；预算截断；坏分支 issue |
| storage 单测 | hierarchy metadata 投影、区间过滤、从 KV 重建索引 |
| 集成测试 | P2 起：write snapshots → HIERARCHY → recall scene/event → expand 到 snapshot → replace span |
| 回归测试 | hierarchy 关闭、空结构和旧 codec 数据下既有路径零行为变化 |

存储测试使用 in-memory Store；stage 算法使用固定 fixture 和规则 stub，不依赖在线 LLM。
模型参与的命名、摘要和语义切分质量另设离线评测，不把非确定外部调用混入单元测试。

## 已知遗留

1. **独立边存储迁移阈值**：多父、多 kind 共节点和边属性复杂度达到何种规模时迁移，
   需要以真实查询与一致性成本评估。
2. **并发一致性**：父子双向更新、`replace_in_span` 与并发 write 的后端事务能力和
   故障恢复仍需实现验证。
3. **父摘要生成质量**：不同 kind 的摘要器、失败降级与幂等性仍需实现阶段验证。
4. **树预算策略**：多父候选间的公平性与深度偏置需要基准评测。
5. **TIME 切分算法**：time_span、scene、event 的边界与置信度策略需要数据集和人工评审。
6. **多 kind 交叉查询**：首期只保证单 kind 遍历，跨 kind 联合过滤与结果合并后置。
7. **扩展分数传播**：衰减和等非 MaxP 算法需在真实浏览场景中验证后再进入契约。

F01 的同 unit 披露设计与实现历史见
[F01-memory-layer.md](F01-memory-layer.md)。
