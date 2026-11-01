# F10 — 记忆重要度与来源标识

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-29 |
| 影响范围 | jiuwen_memory/common/type_def/memory.py，jiuwen_memory/construction/{common.py,classifier_impl,extractor_impl,abstractor_impl,evolver_impl/{orchestrating,dynamic}_evolver.py}，jiuwen_memory/ingest/ingestor_impl/simple_ingestor.py，jiuwen_memory/config/defaults.py，docs/specs/S01-ingest-access.md，docs/specs/S05-construction.md，docs/specs/S07-common.md |
| 测试基线 | 全量 6219 passed / 86 skipped / 4 failed（Python 3.11.6，本地开发机（aarch64））；4 例失败都是本机缺 `watchdog` 库，改动前基线同款，无新增失败。skip 比早先记录的 31 例多，是环境差异：本机缺 elasticsearch、nano-graphrag、torch 等可选库，Milvus、Redis、PostgreSQL 测试服务未启动，相应用例跳过。ruff 0.16.8 无新增告警 |
| 关联特性 | docs/features/retrieval/F09-recency-importance-secondary-rank.md（重要度的消费方） |

> 编号说明：评审稿把本特性称作 construction/F09，但 `F09` 已被
> `F09-agent-llm-extractor.md` 占用；按「序号不复用」取下一个空号 F10。

## 背景

**重要度缺失**。每条记忆本应有 0~1 的重要度用于排序、遗忘、过滤，但改动前唯一写
`system_metadata.importance` 的是降权删除（`DeleteMode.DOWNWEIGHT`，乘 0.5）。新写入的记忆
没有起始重要度，检索无法按重要度排序，降权删除面对空值的行为也不确定。
`architecture.md §17.7` 早已把「重要度应成为显式字段」列为开放问题。

**来源标识缺失**。记忆有两种出生路径：原文直写（`infer=false`）与推断派生（`infer=true`，
从对话抽取）。`provenance` / `source_ref` 能间接推断是否派生，但没有字段直接说明；标记调用
路径的 `infer` / `procedural` / `middle` 又在不传播清单里，派生记忆自身看不出出生路径。

本特性参考 mem0 `memory-triage` skill 的启发式，为记忆产出阶段补这两类元数据。

## 决策

1. **重要度存 `system_metadata["importance"]`，不升级为独立字段**。值是 [0,1] 内两位小数的
   浮点字符串（如 `"0.85"`），与降权删除既有写法一致；索引构建器自动投影
   `system_metadata.<键>` 为可过滤字段，加键不触发序列化版本升级，旧数据缺键时取默认。
2. **LLM 只判「重要度类别」，数值由配置查表；原文与派生两条路径同款**。
   - 默认七类：`identity` 0.95、`rule` 0.90、`preference` 0.85、`knowledge` 0.80、
     `relation` 0.75、`default` 0.50、`transient` 0.30；业务可增删与覆盖。
   - 类别与 tier 正交：tier 回答「是什么类型的事实」，类别回答「有多重要」。
   - LLM Classifier 与各 LLM Extractor 的 system prompt 末尾追加类别段，内容由当前配置表
     生成（业务自定义类别也会列出），要求输出 `importance_category` 字段。类别段受
     `globals.importance_category_prompt_enabled` 控制，**默认关**：关时提示词与加入类别段
     之前逐字节相同，LLM 判类被忽略，重要度一律取 `default` 类别值。
   - KeywordClassifier 不调 LLM，按规则词判类：「永远不要/必须/禁止/一律」→ rule，偏好词 →
     preference，其余 default。「总是」在这里算偏好词（→ preference），与 LLM 提示词口径
     （「总是」→ rule）不同：中文里「总是」多是习惯描述（「他总是喝咖啡」）。
   - 查表规则：命中取对应值；类别缺失、非法或未配置取表内 `default`；表内也没有
     `default` 时取 `"0.50"`。值经 `parse_importance` 钳位 [0,1]，非数值/NaN/Inf/布尔回落
     `"0.50"`，防止配置笔误引入脏值。类别比较忽略大小写与首尾空白。
3. **调用方显式值优先只适用于原文路径**。Classifier 只在 importance 缺失或为空时补写；
   批次 LLM 失败时也补 `default` 对应值，保证 ACTIVE 且内容非空的原文记忆总有重要度。
4. **派生路径恒写自己的 origin 与重要度**。Extractor（llm / agent_llm / dynamic_llm /
   keyword / video_memory / entity_schema）与 Abstractor（concat / llm）产出的新 unit 写
   `origin="inferred"` 与查表重要度。派生 unit 的 `system_metadata` 由
   `inherited_system_metadata(sources)` 取来源交集，再以 `| inferred_attribution(...)` 放在右侧
   覆盖——否则来源同值的 importance/origin 会被继承，类别查表永远不生效。非 LLM 产出方
   没有类别，取 `default`。
5. **来源标识 `system_metadata["origin"]` 二态**：`"original"`（SimpleIngestor 在缺省时写，
   调用方显式值保留）/ `"inferred"`（派生产出方写）。不加入不传播清单，也不是内核保留键。
6. **去重合并时重要度按数值取 max（只增不减）**。OrchestratingEvolver 与 DynamicEvolver
   的 UPDATE：合并前取旧/新两值的 max，`inherited_system_metadata` 跑完后写回；SUPERSEDE：
   新版本落盘前写成 max。任一侧缺失或非法时忽略该侧，双方都缺时不写。
   origin 在 UPDATE 时随被合并的 existing：合并前备份，`inherited_system_metadata` 跑完后写回
   （两侧 origin 不同时交集会丢掉该键）；existing 本身无 origin 时不写，不借候选的值——存量
   数据无从判断出生路径，借候选的 `inferred` 可能把原文误标为派生。SUPERSEDE 落盘的是新版本，
   沿用候选自带的 origin。SchemaOrchestratingEvolver 走父类合并路径的部分随之覆盖。
7. **常量与函数的位置**。`ORIGIN_KEY` / `ORIGIN_ORIGINAL` / `ORIGIN_INFER` /
   `IMPORTANCE_KEY` 与 `ImportanceCategory` 枚举定义在 `common/type_def/memory.py`（接入层
   只依赖 type_def，不反向依赖构建层）；查表、钳位、取 max、prompt 类别段等函数只在构建层
   用，放 `construction/common.py`。
8. **配置放 `globals.importance_category_map`，装配期固化**。各 Classifier / Extractor /
   Abstractor 的 `_build` 经 `config.get` 读取（`ComponentConfig.get` 在 params 缺键时回退
   globals）并存进实例，运行期不再查 config。YAML 覆盖时整表替换。

与评审稿的两处差异（实现前已确认）：

- 评审稿把配置放顶层 `importance.category_map` 段。顶层段名必须是已注册的 Producer
  命名空间，新增一个只装数据的段要么注册空 Producer、要么改解析器白名单；放 `globals` 下
  则零装配改动，且两条路径同样共用一份表。
- 评审稿要求「已有值不覆盖」对所有路径成立。派生 unit 的 importance 几乎总是从来源继承来的
  （见决策 4），若一律不覆盖，派生路径的类别查表形同虚设；因此「不覆盖」只对原文路径上
  调用方显式传入的值成立。

## 拒绝的方案

- **重要度升级为 `MemoryUnit.importance: float` 独立字段**：要改序列化格式、索引字段写入、
  内核保留键与降权删除写法，跨层改动面过大；先在 system_metadata 层验证语义。代价是字符串
  字典序失真，见遗留 1。
- **LLM 直接输出数值重要度（含原派生路径方案、Classifier 五维分类）**：大模型数值输出不稳定，
  且重要度与业务强相关，业务需要能自行配置。
- **调用方启发式 base_importance + LLM 调整的双层方案**：最贴近 mem0，但要把 base 值传入
  抽取上下文，双层语义复杂。
- **按 tier 查表、不引入独立类别**：改 tier→importance 映射会牵动 tier 语义本身，业务无法
  独立调整重要度维度。
- **类别→数值映射硬编码在代码里**：业务改不动。
- **配置放顶层 `importance` 段**：见决策节差异说明。
- **origin 多态**（区分抽取/过程摘要/中期缓冲/schema 属性/抽象）：需求字面是二态，多态需
  枚举化设计且与 `provenance` 重叠。
- **origin 升级为独立枚举字段**：importance 已留 system_metadata，origin 保持一致。
- **origin 加入不传播清单**：本特性由产出方显式写入而非依赖继承，加入清单无益；且原文
  unit 经 UPDATE 合并时仍需保留其 origin。

## 验证

- `tests/unit/construction/test_classifier.py`：LLM Classifier 按自定义表查类别写入、类别大小写
  不敏感、调用方值不覆盖、未知类别取 default、空表取 0.50、批失败取 0.50、prompt 列出自定义
  类别；KeywordClassifier 按规则判类与不覆盖。
- `tests/unit/construction/test_extractor.py`：LLM Extractor 按类别写入且 origin=inferred、覆盖
  继承值、类别缺失/未知、配置值越界钳位、非数值兜底、prompt 末尾类别段、procedural 路径；
  KeywordExtractor chunk（不改源 unit）与 procedural 路径；ConcatAbstractor。
- `tests/unit/construction/test_evolver_dedup.py`：UPDATE 取 max（升、保高、旧值缺失、按数值
  比较、双方缺失不写）、SUPERSEDE 取 max（升、保高）；UPDATE 保留 existing 的 origin（original
  与 inferred 两向、existing 缺失时不借候选值）、SUPERSEDE 沿用候选 origin。
- `tests/unit/construction/test_dynamic_extraction_consolidation.py`：DynamicEvolver UPDATE /
  SUPERSEDE 取 max 四组参数化；origin 合并同上四组参数化。
- `tests/unit/ingest/test_simple_ingestor.py`：缺省写 original、调用方 origin/importance 保留、
  常量拼写。
- `tests/unit/config/test_defaults.py`：出厂七类表与代码常量一致；覆盖 globals 表后经装配读到
  新值、未列出的类别回落新表 default。
- 既有用例随新字段更新断言：`test_hierarchy_hints.py`（多出 origin）、
  `test_metadata_separation.py`（多出 origin 与 importance）、`test_agent_llm_extractor.py`
  （中文 prompt 末尾多出类别段）。

## 已知遗留

1. **`architecture.md §17.7` 未根治**：重要度仍是字符串，跨长度字典序比较失真（`"0.5"` >
   `"0.95"`）；索引侧数值范围过滤需后端隐式转换。根治需升级为独立数值字段。检索侧次级
   排序已在读取侧按数值解析（retrieval F09）。
2. **默认 infer 路径没有类别信号**。出厂 extractor 是 `dynamic_llm`，未配置 prompt 策略时
   回落 `keyword` 抽取器，派生记忆一律取 `default`（0.50）；`dynamic_llm` 使用业务自定义
   策略 prompt 时，prompt 里也没有类别段。要让派生重要度有区分度，须使用 llm / agent_llm
   抽取器，或在自定义策略 prompt 中要求输出 `importance_category`。
3. **类别段会改变抽取产物，所以默认关**。F09 系统测试 M5 不通过：打开后 Extractor 产出
   与无类别段时明显不同（content 一致度 0.68 → 0.24），而 importance 仍有 93.6% 是
   `default`。只测了偏离，没有参考答案，好坏未定。质量评估之后再决定是否打开。
   后续方向：类别判断拆成独立 LLM 调用，Extractor 提示词不动，代价是多一次调用。
4. **Schema 专用的 update / property merge 路径未接入取 max**（`schema_update.py`、
   `schema_property_merge.py`），均未显式处理重要度取 max 与 origin 保留：前者
   `{**before, **candidate}` 由新候选覆盖，后者 deepcopy 后原样带过，均为覆盖语义，键不会丢，
   只是不取 max、也不保留 existing 的 origin。新增 Evolver 实现若自写合并也须同步。
5. **origin 仅二态**，多态扩展需与 `provenance` 一并设计；值域为字符串，可向后兼容扩展。
6. **部署档 Classifier 未接 LLM，原文路径重要度恒为 0.50**。local / online 部署档的
   `classifier.default` 是 `llm` 且不带参数，装配出来的是不调模型的匿名 echo，不判类别。
   这是本特性之前就有的部署档缺口，与本特性无关；接上 LLM 会让每次 add 多一次模型调用，
   属于独立的行为变化，另行修复和评审。
