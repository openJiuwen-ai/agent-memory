# F10 — 经验记忆与技能沉淀

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-19（写入链路）、2026-10-22（周期整理 / 技能归纳 / 判定更新链） |
| 影响范围 | `jiuwen_memory/construction/`（appraiser.py / appraiser_impl/ / curator.py / curator_impl/ / abstractor.py / abstractor_impl/ / experience.py / extractor_impl/experience_extractor.py / base.py / evolver_impl/ / bootstrap.py / common.py / prompt_registry.py / prompt_strategy.py / router.py）、`jiuwen_memory/common/experience.py`、`jiuwen_memory/common/`（type_def/raw.py、security/types.py、security/space_decision.py）、`jiuwen_memory/api/memory_api_impl/`（assembly / write_ops / pep_ops / query_ops / dreaming / local_memory_api）、`jiuwen_memory/control/`（types / kv_space_manager / space_aware_permission_manager / cloud_engine / pipeline / metadata_pipeline / application/space_lifecycle.py / jobs_impl/evolve_job.py / engine_impl/ / scheduler_impl/）、`jiuwen_memory/storage/kv_impl/encrypted_kv_store.py`、`jiuwen_memory_entry/core/handler.py` |
| 测试基线 | `pytest -m unit` 全量通过；分文件见各部分「验证」；ruff 全绿 |
| 组成 | 本文合并原「经验记忆写入链路」「经验记忆的周期整理」「技能归纳与使用反馈」「判定更新链完整版」四份文档（原 F09-F12，construction 域）；经验检索独立归档在 [retrieval/F09](../retrieval/F09-experience-retrieval.md) |

## 背景

mem2.0 的经验记忆让 agent 从执行轨迹中沉淀可复用做法：轨迹传入后先评价（成败
判定、归因），值得提炼的轨迹再抽取为经验条目落入共享的经验空间，供后续任务
检索复用；多条独立复现的同类经验周期归纳为技能（一份 `SKILL.md`），审核后同步
到 agent 的技能目录。

```
写入链路（第一部分）：轨迹传入 → 评价八步 → 提炼 → 落盘 → 收尾
检索（retrieval/F09）：经验空间的 search 走 Selector 选取（复现要求/排序分/探索名额）
周期整理（第二部分）：Dreaming 周期触发 → 聚合/退休/合并/淘汰/控规模 → 按作者清除
技能归纳（第三部分）：整理之后的条目分组归纳 → SKILL.md 草稿 → 周期维护/使用反馈
判定更新链（第四部分）：判定被事后新证据推翻时补做——补写复现事件、翻转时重新归因
```

四部分的依赖：写入链路是地基（先有轨迹判定与条目落盘，其余才有输入）；周期整理
消费写入积累的效用事件、清理共享空间的条目；技能归纳只用整理后留下的条目；
判定更新链把写入时留下的判定记录读取方（补写复现、重新归因）接上。

agent-core 侧（openjiwen 仓库）的轨迹传出不在本仓库。

## 目标

1. 轨迹传入 → 条目落盘 → 收尾的全链路在现有 evolver 演进框架内闭环，不改
   `MemoryAPI` 的方法签名。
2. 评价结论（轨迹记录、判定、归因）经抽取上下文传给抽取算子，不落 MemoryUnit
   公开字段。
3. 共用部件（记录读写、报错规则、消息压缩、密钥清洗、作者摘要）对评价与抽取
   两个算子单点维护。
4. 经验空间的读收紧（derived_only）与经验写入的落点鉴权在 API 层生效。
5. 轨迹记录与对话原文同级别加密（AAD purpose 同类）。
6. 共享经验空间的条目有周期维护闭环（聚合、退休、合并、淘汰、控规模）与
   按作者清除的合规路径。
7. 技能沉淀：多条经验归纳为 SKILL.md 草稿、技能的周期维护与使用反馈。
8. 判定记录的事后更新（补报、反馈回填）带动效用数据修正。

## 非目标

- 经验检索（Selector 经验路由、pipeline_profiles 检索侧）——独立归档在
  [retrieval/F09](../retrieval/F09-experience-retrieval.md)。
- `derived_only` 空间的**写入拦截**（add 直接写入拒绝）：只做读侧收紧与
  经验写入的落点鉴权。
- agent-core 侧（openjiuwen 仓）的轨迹传出 Rail 接线与真实 agent 执行级
  端到端。本仓 adapter（`jiuwen_memory_adapter/jiuwenswarm`）已交付
  provider 侧的轨迹转交（`Mem2TrajectoryStore` / `add_trajectory`）与技能
  同步（`sync_skills`）；内核全链路另有真实 LLM e2e
  （`tests/unit/api/test_experience_e2e_real_llm.py`）。

---

## 第一部分：写入链路

```
轨迹传入（add 携带轨迹 payload）
  → Appraiser 八步的前五步（重复检查/结构过滤/判定反馈/判定成败/归因）
  → ExperienceExtractor 提炼（第 6 步，值得提炼时）
  → 现有去重判定与落盘（第 7 步）
  → finalize 收尾（第 8 步：复现事件/计数/轨迹置完成）
```

### 决策

#### 一、评价算子是独立可选算子（`construction/appraiser.py`）

形式与 `router.py` / `layer_annotator.py` 相同：契约（`Appraiser` + `Appraisal` +
`AppraiserProducer`）在顶层接口文件，实现（`appraiser_impl/`）以
`@AppraiserProducer.register("<名>")` 自注册；profile 不配置即整段跳过——
evolver 未取到实例时不接线，与现有可选装配同形。

分工：`appraise(units)` 在抽取**之前**做准入与评价，允许副作用（读写专用记录）；
`finalize(units, appraisal, result)` 在流程结束时收尾一次。`Appraisal.proceed`
为假时本次写入到此结束：不抽取、也不调 `finalize`（轨迹状态由 `appraise` 自己
置好）；`Appraisal.data` 类型由实现定义，经验实现是 `ExperienceAppraisal`，
经 `ExtractContext.appraisal`（`base.py` 新字段）传给抽取算子。

#### 二、evolver 接线：评价包住抽取（`orchestrating_evolver.py` 1087-1168）

`_evolve_extract` 的前半段改为：

```
appraisal = self._appraise(units)        # 未配置 appraiser → None，行为与旧版一致
if appraisal is not None and not appraisal.proceed:
    return EvolveResult()                 # 短路：不抽取、不收尾
try:
    result = self._extract_with_appraisal(units, appraisal)
except BaseException:
    self._finalize(units, appraisal, None)   # 异常也要收尾（置失败），再 raise
    raise
self._finalize(units, appraisal, result)
```

三条铁律（由 `test_evolver_appraiser.py` 20 用例固定）：

- `_appraise` 异常照常冒泡：不进抽取、不收尾——评价失败时评价算子自己已把
  轨迹状态置为失败，收尾反而会把失败改写成其他状态。
- `_finalize` 自身异常**只记 warning 不抛**：收尾发生在条目已落盘之后，抛出会把
  成功写入误报为失败。
- `_extract_with_appraisal` 里 appraisal 非 None 而收集到的 context 为 None（非
  infer 写入）时补一个空壳 `ExtractContext` 挂上 appraisal——保证抽取算子拿得到
  评价结论。

`DynamicEvolver` 覆盖版同形接线（procedural 路径走父类）。

#### 三、共用部件合成顶层单文件（`construction/experience.py`）

评价与抽取共用的部件按六节组织在同一文件：**记录**（键构造 + 轨迹/判定/效用
三个 Store）、**报错规则**（tool_error 级证据与恢复判定）、**轨迹消息**（读取
位置与压缩折叠）、**密钥清洗与脱敏检查**、**作者摘要**（HMAC）、**评价结论**
（`ExperienceAppraisal`）。测试对应合并为 `test_experience.py`（88 用例）。

> 经验检索（[retrieval/F09](../retrieval/F09-experience-retrieval.md)）更新：
> 效用域部件（效用事件、统计记录、`UtilityStore`、策略取值、质量分、`usage_ref`）
> 已拆至 `common/experience.py`——检索侧选取算子也要读，分家线是读者；
> 本文件保留构建侧专用部件。

#### 四、键布局与作者摘要

| 记录 | 键 | 空间 | 保留期 |
|---|---|---|---|
| 轨迹记录 | `/trajectories/{作者摘要}/{tid}` | 写入落点（作者空间） | 30 天 |
| 判定记录 | `/verdicts/{作者摘要}/{tid}` | 同上 | 同期 30 天 |
| 归因/复现事件 | `/experience/utility/{条目id}/{attr\|repro}:{摘要}:{tid}:{修订号}` | 经验落点空间 | 持久 |
| 复现作者 / 类别计数 / 统计 | `/experience/repro-authors/` / `/experience/counts/` / `/experience/stats/` | 经验落点空间 | 持久 |

- **事件键含判定修订号且只 `insert`**：同一事件重复写入（断点续做、重发）报
  `ConflictError` 即已记过，`UtilityStore` 返回 `False` 幂等。
- **`TRAJECTORIES_KEY_PREFIX` / `trajectory_key` 放 `common/type_def/raw.py`**：
  加密 KV 按键前缀归类加密 purpose（`storage` 不能 import `construction`），
  键布局必须在 type_def 层；其余前缀常量只被 construction 消费，留在
  `experience.py`。
- **作者摘要**：作者主体（`user:<id>` / `agent:<id>`）经部署密钥
  `experience_author_key` 做 HMAC-SHA256（`security/types.py` 的
  `SECRET_PARAM_KEYS` 登记，可 `${VAR}` 注入，未配置时经验 profile 装配失败）。
  不复用 `KeyProvider.mac`——它绑定当前活动密钥、随轮换变化，而作者摘要要求
  「部署后固定不换」（跨用户复现计数与来源轨迹定位的前提）。密钥不随加密密钥
  轮换；摘要只用于拼键，不要求可逆。

#### 五、ExperienceAppraiser（八步中的 1-5 与 8）

`appraise` 按序执行：重复检查（轨迹记录已存在且已处理完 → 只按外部结果补报
更新判定，不重复产出）、结构过滤（非法消息数组拒绝）、判定反馈（回填上一条
轨迹的判定）、判定成败（写判定记录，证据等级 external > user_feedback >
tool_error > model_only，高等级取代低等级）、归因（写归因事件）。`finalize`
执行第 8 步：对新增与判为重复命中的条目写复现事件、更新复现作者记录（至多
2 个）与类别计数、把条目 id 记入判定记录、轨迹置已完成；`result` 为 `None`
（流程异常）时置处理失败。

轨迹请求可带 **context 背景块**：预定义字段 turn / started_at /
ended_at / end_state / project / model / framework / extra，未定义字段、
类型不符（turn/started_at/ended_at 整数、end_state 枚举、project/model/
framework 字符串、extra 对象）与 extra 超限（> 32 键、键名 > 64 字符、
字符串值 > 256 字符、数组元素非字符串、值非标量或字符串数组）在结构过滤
拒绝。轨迹记录带生产者主体 `producer` 与凭据指纹 `credential_id`（从接口层
注入的内核保留键与 `scope.org` 组出，见决策八）、context 原样落盘——判定
反馈的会话核对按它的 session 维与 turn 比对（第四部分）。

值得提炼的判定（四条之一）：任务成功、工具调用次数达阈值（缺省 5）、
轨迹中有报错（含已恢复——t-0001 的配置缺失即属此类）、外部验证通过。
一次沉淀的处理时限缺省 150 秒，各步开始前检查；失败可重试至多 3 次。

#### 六、ExperienceExtractor（第 6 步）与三道落盘闸

值得提炼时至多三次模型调用（提炼、修正、分类）产出候选 `MemoryUnit`，交给
现有流程做落点判定、去重判定与落盘（第 7 步不经新代码）。只提炼观察到成功的
做法，失败的教训改写为正向做法。环境事实（fact）与条件动作（tip）各是一个
抽取策略（`prompt_strategy.EXTRACTION_STRATEGY_KEY`），供去重判定按策略取规则。

落盘前三道闸都在本算子：可执行标记（确定性规则 `is_executable_action`）、
脱敏检查（正文命中主体相关取值即整条丢弃，仓库相对路径不误伤）、类别写入
上限（达 850 的类别候选丢弃并记 dropped 计数）。条目不属于任何用户：scope 只
填组织与经验空间，作者标记固定 `system:experience`；来源轨迹不记在条目上，
由评价算子的 `finalize` 记复现事件。

#### 七、去重判定的策略范围（`DynamicEvolver.dedup_decisions`）

新增装配参数 `dedup_decisions`：按抽取策略名声明允许的去重结论，候选带策略
元数据且策略命中声明时，LLM 判定结果超出范围一律回退 ADD（含
existing/score 保留）；未声明策略或未配置时不限定。范围外 UPDATE/SUPERSEDE 缺
existing_id 时 `_judge` 捕获回退 ADD。NOOP 决策把已有条目 id 记入
`result.duplicate_ids`。解析（`_parse_dedup_decisions`）对非映射/空列表/非法
结论抛 `ValidationError`。

#### 八、经验部署配置、落点鉴权与生产者标记（api 层）

- **配置解析**（`experience.parse_experience_config`）：`globals.experience`
  声明 `memory_class`（经验写入标为的判定表类别，缺省 `experience`）、
  `default_space`（不带 `coords` 的写入落点：`own` 或空间名，缺省 `own`）与
  `curated_spaces`（做周期整理的空间清单，每空间一项 space / skill /
  skill_release / repro_unit）；非法形态在装配期抛 `ValidationError`。
- **落点鉴权**（`write_ops._authorize_experience_space`，add 与 add_async 都
  调）：经验类别（`memory_type` 落在 `NO_FALLBACK_MEMORY_TYPES`）且**不带
  `coords`** 的写入，缺省落点由 `default_space` 给出——为空间名时对该经验
  空间（主体同组织 + 该空间名）单独按 WRITE 鉴权，无权返回权限错误；为
  `own` 时落点即写入落点（现有逻辑已校验，空过）。带 `coords` 的写入由落点
  判定计算候选空间时校验。未配置经验的部署不在此报错（引擎的 profile
  准入兜底拒绝）。
- **生产者标记注入**（`write_ops._with_producer_marks`）：经验类别的
  写入把认证结果的生产者主体（从 `security.auth.actor` 推导）与凭据指纹
  （`AuthContext.credential_id`，空串恒写键）写进 `system_metadata` 的内核
  保留键 `_producer_principal` / `_producer_credential`，覆盖请求同名键——
  调用方自报的接入方标识不可信，补报核对（第四部分）按生产者取值。两个键属
  `KERNEL_SYSTEM_METADATA_KEYS`，调用方经入参写入在边界先被拒（先拒后写，
  与 `_with_author_marks` 同一次序）。主体维全空的调用方（运维通道）跳过
  注入——经验写入在评价算子按「缺少生产者标记」拒绝。

#### 九、derived_only 空间的读收紧

`SpacePolicy` 新增 `write_mode`（`any` 缺省 / `derived_only`，`control/types.py`）。
`pep_ops._apply_space_policy_context` 在 `write_mode=="derived_only"` 且入口为
`get`/`list` 时，把缺省空间动作改判为 UPDATE 并关闭「本人所写」附加集合
（`own_actions_apply=False` 经 `PermissionContext.metadata` 通道传递，
`SpaceAwarePermissionManager._own_actions_apply` 读取覆盖 `EntryRule` 的开关，
显式 `space_action` 保留）。效果：derived_only 空间的 `get`/`list` 只对 editor
放行（contributor 的 UPDATE 仅作用于本人所写条目的附加集合，被关闭）。

#### 十、轨迹键的加密归类（`encrypted_kv_store`）

`_purpose_for_key` 把 `/trajectories/` 前缀与 `/messages/` 同归 `raw_message`
purpose：轨迹记录与对话原文同为「对话原文」性质（含用户消息明文），AAD 与原文
消息一致；purpose 不一致时 AAD 不匹配、解不开。

### 拒绝的方案

- **共用部件做成包 `construction/experience/`（六子模块原案）**：
  被拒。消费方只有两个 impl 目录，跨 impl 复用没有先例；单文件后消费方 import
  合并为一条（`from jiuwen_memory.construction.experience import ...`），目录与
  `__init__.py` 导出问题一并消失；仓库千行文件有先例（orchestrating_evolver
  1302 行）。六模块 docstring 转为文件内分节注释，接口零变化。
- **每模块一个测试文件**（test_records/test_scrub/test_signals/test_messages）：
  被拒，随源码合并为单个 `test_experience.py`（88 用例守恒）；两个算子测试
  （test_experience_appraiser / test_experience_extractor）保留独立——各自镜像
  其源码文件。
- **复用 `KeyProvider.mac` 算作者摘要**：被拒（见决策四）。进程内随机 HMAC
  密钥与绑定活动密钥的实现都不满足「部署后固定」。
- **评价失败时也调 finalize 统一收尾**：被拒。appraise 抛异常时评价算子已把
  轨迹状态置为失败，再走 finalize 会把失败改写；契约明确「抛出异常之前，实现
  自己写入的状态须已置为失败」。
- **finalize 失败上抛**：被拒（见决策二铁律 2）。
- **`derived_only` 随写入链路即做写入拦截**：被拒，范围裁剪——经验 profile 之外无
  派生写入路径，先由读收紧 + 经验落点鉴权覆盖实际暴露面。
- **`_parse_dedup_decisions` 装配校验做单测**：被拒。`EvolverProducer` 完整装配
  依赖链逐级缺注册（LlmProducer echo → ChunkerProducer fixed_window），单测搭
  不起来，属集成测试范畴。

### 验证

`pytest -m unit` 全量通过（500 个非 unit 标记 deselect），分文件：

- `tests/unit/construction/test_experience.py`：88 用例——记录节 25（键布局、
  三 Store 往返/幂等/TTL/上限）、报错规则 16（恢复匹配边界：t-0001 包含恢复、
  短命令不做包含、工具名不同不恢复）、轨迹消息 24（读取位置、压缩折叠）、
  密钥清洗与脱敏 23（三道正则链、嵌套清洗、词边界与目录命中不误伤）。
- `tests/unit/construction/test_experience_appraiser.py`：评价算子全链路（八步
  前五步、值得提炼四条、证据等级、归因、finalize 各情形表）。
- `tests/unit/construction/test_experience_extractor.py`：提炼/修正/分类三调、
  三道闸、scope 与作者标记。
- `tests/unit/construction/test_evolver_appraiser.py`：20 用例——evolver 接线
  铁律（三异常路径、空壳 context、finalize 吞异常）与 dedup_decisions 八态。
- `tests/unit/api/test_experience_space_authorization.py`：27 用例——落点鉴权
  七情形 + 配置解析六情形 + 生产者标记注入六情形（认证结果覆盖请求自报键、
  非经验写入不带键、空主体跳过、空凭据恒写键）。
- `tests/unit/construction/test_experience_appraiser.py`：80 用例——含 context
  块校验（非法字段/类型/extra 超限拒绝、合法 context 落盘轨迹记录）与补报
  生产者核对（第四部分）。
- `tests/unit/api/test_space_aware_authorization.py`（追加 2）：derived_only 端
  到端（contributor get/list 拒、editor 过）与 `own_actions_apply` 通道。
- `tests/unit/common/security/test_space_decision.py`（追加 2）：own_actions
  开关对 contributor/editor 的判定。
- `tests/unit/storage/test_encrypted_kv_store.py`（追加）：轨迹键 AAD purpose
  归 raw_message。

开发中被单测拦下并修复的三个实现缺陷：scrub 的 JSON 赋值形态漏清洗
（`_SECRET_ASSIGNMENT_RE` 分隔符组补引号）、`add_repro_author` 返回值与落盘内容
不一致（截断后的列表）、`_append_event` 的 `ConflictError` 构造参数错误。

---

## 第二部分：周期整理（Curator）

写入链路让经验条目落进共享经验空间、经验检索让检索侧按效用选取。共享空间的
条目会持续积累：内容重复、互为替代、过于宽泛、有害或闲置的条目没有人清理；
效用事件与统计记录只增不减；用户删除自己的空间后，他在共享经验空间留下的
效用事件与「只剩他复现」的条目仍是可被间接感知的痕迹。

周期整理按轮执行「聚合 → 退休 → 合并删除 → 竞争淘汰 → 控规模 → 类别重数」，
整理排在归纳之前（第三部分的 SkillAbstractor 只用整理后留下的条目）。同批
交付 Dreaming 周期任务的三处配套改动与按作者清除（合规路径）。

```
Dreaming 周期触发（CONSOLIDATE）
  → Evolver._evolve_consolidate：可选 Curator.curate（整理）→ abstract（归纳）
  → ExperienceCurator 第 0 步 按作者清除（消费清除标记）
     第 1 步 聚合（效用事件 → 统计记录 + 类别基线）
     第 2/3 步 退闲置 / 有害退休
     第 4 步 合并与删除（聚簇 + 模型三结论，可选）
     第 5/6 步 竞争淘汰 / 控制规模
     第 8 步 类别重数（第 7 步技能归纳属第三部分）
```

### 决策

#### 整理流程（ExperienceCurator）

1. **入口筛只收生效的经验条目**：`ACTIVE` 生命周期 + `memory_type` 为
   `experience` + tier ∈ {SEMANTIC, PROCEDURAL}；同空间的技能条目
   （`memory_type=skill`，第 7 步自行维护）、对话条目（不带
   `memory_type`）与运行摘要（EPISODIC）都不参与。
2. **统计随聚合全量重算**：`aggregate_utility` 从事件流全量重算每条条目的
   统计记录，`prior` 只带过聚合不产生的字段（`merged_into` /
   `competition_group` / `sealed`）。这是按作者清除能「删事件即重算」的前提
   ——没有独立的统计修订逻辑。
3. **退休 = FORGOTTEN + SOFT remove**：条目标记退出检索、本体与统计记录
   保留（与 `_evolve_forget` 同款两条指令）。合规与审计要求可追溯。
4. **第 4 步模型可选**：LLM 或 Embedder 未装配时只做三段不全的规则退休，
   整理不因缺模型配置而整轮失败。三结论（merge/compete/too_broad）一簇一次
   调用；结论重叠时过于宽泛 > 合并 > 竞争。
5. **失败隔离两级**：第 0 步（按作者清除）与第 1 步（聚合）失败本轮中止
   （后面各步都依赖统计记录；清除没做就把旧事件聚回去等于把被清除作者的
   影响又算一遍）；第 2-6、8 步各自捕获异常，失败的一步记入运行摘要并
   跳过。任一步失败时本轮不归纳（返回空列表）。簇级失败再隔离一层：一簇
   的模型调用重试后仍失败只跳过该簇。
6. **运行摘要落 EPISODIC 条目**（`memory_type=experience`、标签
   `run-summary`、`FORWARD_ONLY` 只写本体），`search` 取不到、编辑者用
   `list` 按 `memory_type` 与标签可查。
7. **空间清单过滤**：`globals.experience` 的 `curated_spaces` 配置后只处理
   清单内空间；清单外不整理、不写摘要、不进返回集——返回集是「参与后续
   归纳」的条目。覆盖全组织的注册会读入全库条目，过滤挡住非经验空间混入
   归纳。
8. **`repro_unit` 逐空间解析、trajectory 口径的空间逐个关闭作者限计**：
   装配期把每个 `curated_spaces` 空间的 `repro_unit` 解析到底（auto 按
   落点，见第三部分「方案改版」），trajectory 口径的空间集 `trajectory_spaces`
   传入 Curator，`_window_days_for` 对这些空间返回 None（不限计）、其余
   沿用部署限计窗。两种错误代价不对称——误关（部署按轨迹计数却被按作者
   限计）只是轻度高估，误开会让单用户部署的复现轨迹数被压到时间窗内至多
   一条；初版曾按「任一 trajectory 声明即整体关闭」实现，后改逐空间——
   同库混布 user/trajectory 口径空间时整体关会让 user 口径空间失去限计。

#### 合并产生的条目

- 内容沿用模型给的合并正文（命中部署配置的主体标识清单即放弃该组、旧条目
  保留——防模型在重组正文时引入主体标识）；
- 类别/三段/tier 沿用、`provenance` 记被合并条目 id、落盘时间取最早（不进
  隔离期）；
- 统计继承：计数相加、作者并集、时间取 max；`merged_into` /
  `competition_group` 不继承（新条目无去向、合并即退出竞争）；
- **复现作者记录取被合并条目的并集**（`save_repro_authors` 内部截
  `REPRO_AUTHOR_LIMIT`）——按作者限计读合并后的记录判断「已有两个独立
  作者复现」。

#### 按作者清除（合规路径）

登记（用户空间删除时）→ 清除（下一轮周期整理聚合之前）：

1. `SpaceLifecycleService` 加可选删除后回调组 `on_deleted`（缺省空 = 现有
   行为）；**owners 在元数据删除之前读出**——`SpaceManager.delete` 之后
   就读不到了。
2. 回调实现在 construction 侧（`make_erasure_callback`）：被删空间每个
   owner 的作者摘要（`author_digest`，HMAC-SHA256）写进该组织**每个**经验
   空间的 `/experience/erased-authors/{摘要}` 标记。
3. Curator 第 0 步消费标记：删这些作者的效用事件（统计随聚合重算）、
   退休「复现作者只剩被清除作者」的条目（其他作者也复现过的不动——他们
   的复现是条目继续生效的独立证据）、删标记。退休不过滤 tier：清除是
   合规操作。
4. 清除范围明确不含：经审核技能（同空间后天然豁免——技能条目不进 curate
   units，见第三部分「按作者清除豁免」）、封存累计计数（`sealed`）、复现作者
   记录本身（HMAC 摘要不可逆、定位不到作者，设计只要求删事件/退休条目/
   删标记）。

#### Dreaming 三处配套改动

1. **分组演进**：`EvolveJob.evolver_for_unit` 逐条选取 evolver、按实例分组
   分别演进、六字段结果合并；空桶与未配置选择器仍整批交全局 evolver。
2. **间隔连续计时**：`first_delay`（None = 等一个 interval）+
   `DreamingEntry.last_triggered`（提交演进成功后 `note_triggered` 回写，
   generation 不匹配跳过防旧代覆盖）；重启 restore 按最后触发时间换算
   first_delay。
3. **技能空间复核**（初版配对技能空间形态，已废除）：CONSOLIDATE 注册
   与执行前对配对的技能空间再鉴权；失败用普通异常
   `SkillSpaceUnavailableError`，语义是 FAILED + last_error + 定时器继续，
   区别于 `PermissionDeniedError`（停父定时器 + 注销注册）。技能与经验
   同空间后，经验空间的鉴权已覆盖同空间的技能读写，复核路径与该异常
   一并拆除。

### 拒绝的方案

- **统计增量维护**（事件到达时更新统计记录，聚合只做校正）：拒绝。事件是
  insert-only 的真源，统计记录可随时从事件流全量重算——增量维护引入双写
  一致性问题，且按作者清除需要「删事件即减计数」的语义，全量重算天然满足。
- **erasure 落 `construction/experience/erasure.py` 包**（设计文档的形态）：
  拒绝。现有经验部件是 `construction/experience.py` 单文件模式（第一部分），
  登记回调只是一个函数，拆包增加目录层级没有对应收益。实际落点为
  `construction/experience.py` 的 `make_erasure_callback`。
- **erasure 回调失败上抛让 delete_space 重试**：拒绝。`SpaceManager.delete`
  对已删空间抛 `NotFoundError`（`_require_space`），不幂等——回调异常上抛
  会让调用方对已完成的事务无限重试。标记写入幂等（同键覆盖），回调失败记
  ERROR 日志由部署侧补登记。
- **清除标记读取时机放在第 1 步聚合之内**：拒绝。聚合失败会把「标记已
  消费但事件未删」或「事件已删但标记未删」的中间态混进同一轮；独立的
  第 0 步让「删事件 → 退休条目 → 删标记」成为一个原子步骤（步骤内不再
  分支），失败整体留到下一轮。
- **技能空间复核失败复用 `PermissionDeniedError`**（同空间改版后
  该复核已拆除）：拒绝。Dreaming `drain_queue` 对 `PermissionDeniedError`
  的语义是停父定时器 + 注销注册（用户主动撤销授权的场景）；技能空间不可
  用（未配权限、空间封存）应是可恢复的 FAILED 轮次，
  `SkillSpaceUnavailableError` 落通用异常分支。
- **`last_triggered` 在 tick 开始时回写**：拒绝。提交失败也回写会让失败的
  轮次白等一个完整 interval；只在提交演进成功后回写，失败轮次重启后
  first_delay=0 立即重试。
- **密钥读取只认 `appraiser.experience` 实例**：拒绝。评价算子的实例名不
  固定（evolver 实例参数引用名或 `default`），只认固定名会漏掉合法部署。
  实际读法：扫 `appraiser` 命名空间全部实例的 params，回退 globals 的
  `author_key`——与评价算子 `config.get("author_key")` 的合并语义在单实例
  部署下完全一致。

### 验证

- `tests/unit/common/test_experience_utility.py`：效用事件键/聚合规则/作者
  限计窗口/重定向/复现作者上限/清除标记读写/按作者删事件（含统计与复现
  作者记录不受影响）。
- `tests/unit/construction/test_experience.py`：作者摘要 HMAC 确定性、主体
  推导、登记回调（多 owner 多经验空间、无主体跳过、组织隔离、幂等）。
- `tests/unit/construction/test_experience_curator.py`：入口筛、聚合（含
  作者限计窗与 trajectory 口径空间关限计）、退闲置/有害、合并（构造/统计
  继承/复现作者并集/脱敏拦截/幻觉 id/事件重定向）、竞争淘汰、控规模、
  类别重数、失败隔离、空间清单过滤、按作者清除（事件删除/条目退休/统计
  重算/标记删除/失败中止）、运行摘要（含 `memory_type=experience`）。
- `tests/unit/control/test_application_ports.py`：SpaceLifecycleService 删除
  事务（含 owners 删除前读取、回调异常不传播不拦后续、事务失败不触发、
  元数据缺失时空 owners）。
- `tests/unit/control/test_evolve_job.py` / `tests/unit/api/test_dreaming_coordinator.py`：
  分组演进与结果合并、first_delay 换算、last_triggered 回写。
- `tests/unit/api/test_experience_space_authorization.py`：装配注入（erasure
  回调的 appraiser-params / globals 两条路径与两个关闭条件）与经验写入的
  生产者标记注入（见第一部分）。

---

## 第三部分：技能归纳与使用反馈（SkillAbstractor）

周期整理交付后，经验条目有了维护闭环。技能沉淀交付 mem2.0 侧全部：把多条
经验归纳为多步骤的技能（SkillAbstractor）、技能的周期维护（Curator 第 7
步）、技能的使用反馈（Appraiser 从轨迹识别加载并归因）。技能是来源经验所在
空间中 `memory_type` 为 `skill` 的条目（技能与经验同空间，配置
`globals.experience` 的 `curated_spaces` 各空间 `skill` 字段，为真即开启），
不经检索使用——同步到 agent 技能目录、按名加载（接入适配，不在本仓库）。

```
写入轨迹（Appraiser 第 1-5 步）
  → 第 5 步归因：取用的经验 + 轨迹中加载的技能（skill_tool 调用）同一次
    模型调用判定；技能事件写经验落点空间、键 skill:{名}:{版本}
周期整理（Curator 八步，第二部分）
  → 第 7 步技能：聚合技能事件 → 计数回写条目元数据 → 有害退休 →
    试用转正 → 旧版取代；运行摘要记数
归纳（CONSOLIDATE 模式，整理之后）
  → SkillAbstractor.abstract：分组 → 五条件 → 逐组一次模型 → 三项校验
    → 技能草稿（draft / trial）落本空间，不过语义去重
```

### 决策

#### dedup_products 契约（Abstractor 接口扩展）

`Abstractor` 加类属性 `dedup_products: bool = True`：产物是否先与落点空间
已有条目语义去重再落盘。缺省保持去重（事实/画像类产物语义相同即重复，
向后兼容）；`SkillAbstractor` 置 `False`——同一技能的新版与旧版内容天然
相近，语义去重会把新版误判成重复，技能的新旧版本靠条目 id 幂等与版本号
区分。`OrchestratingEvolver._evolve_consolidate` 在 `_annotate_layers`
之后读本属性：置假时跳过 `_dedup_batch` 直接 `_persist`（先例同 EXTRACT
的 procedural 路径）。

#### SkillAbstractor

注册名 `skill`（`AbstractorProducer.register`，经验 profile 的 evolver
`abstractor` 字段引用）；落位 `construction/abstractor_impl/skill_abstractor.py`。
构造收 `experience: ExperienceConfig`（技能开关与 per-space 的
`skill_release` / `repro_unit` 都从 `curated_spaces` 取）与 `space_naming`
（repro_unit 的 auto 按落点解析用，装配从判定表 `globals.route_table` 取，
见「方案改版」）。`abstract` 四步：分组 → 条件检查 → 生成 → 校验。

**分组**：类别分桶 → 桶内正文余弦 ≥ 0.6 连边（Embedder）→ BFS 连通分量
→ 超 50 条按与组内首条相似度降序贪心分片；两条都是条件动作（三段齐全）
时连边还要求 capability 相同（同簇但能力名不同的不组）。

**条件检查（五条件须同时满足，每轮至多 20 组、新证据多的优先）**：复现
下限（`repro_unit` 口径：user 计数独立复现作者 ≥ 2，trajectory 计数独立
复现轨迹 ≥ 3；`auto` 已在装配期按落点解析到底，判定处只认 user /
trajectory；独立复现 = retrieved_ids 不含组内任何经验）、可靠成功证据
（≥ 1 条 success + external/user_feedback 且成功比例 ≥ 0.5）、有新证据
（prev 非空时须有晚于其 evidence_cutoff 的复现事件）、驳回后有新增复现
（prev lifecycle=forgotten 时复现用户数 ≥ prev.source_users + 2）、效用
不低于基线（组内条目 validity 合计 ≥ 本空间的 baseline[category]，无数据
视为满足）。

**生成**：每组选至多 5 条来源轨迹（按成功、证据等级、新近排序，每个用户
先各取其优先级最高一条再按序补足）→ 逐条 `TrajectoryStore.load` 点读 →
每组一次模型调用（`skill_max_attempts` 次重试，缺省 3）。首版约定：

- **交给模型的轨迹内容**是工具调用序列（`tool_call_digest`）+ 判定结论 +
  证据等级，不含 messages 全量（太大且含与归纳无关的正文）；轨迹已过期
  时该条 `tool_calls` 为 null、仅凭经验归纳。
- **提示词**含组内经验（id/正文/类别/三段）、选出轨迹、本空间已有技能
  名（新技能不得重名）、prev 的 SKILL.md（在其基础上修改、保持名称语义）。
  七项分项输出：name/description/tools/scenarios/preconditions/steps/
  failures；name 与 steps 均非空才接受，空对象不重试直接放弃该组。

**校验（三项）**：

- **技能名**：`^[a-z0-9]+(-[a-z0-9]+)*$` 且 ≤ 64；先最小规范化（lower /
  下划线转连字符 / 去首尾连字符），仍不合规丢弃整组；与已有技能重名加
  `-2`/`-3` 序号（序号至 99）。
- **脱敏**：SKILL.md 命中部署配置的主体标识清单即丢弃。
- **命令覆盖**：步骤命令（**提取**：反引号片段优先，无则退到英文 token
  ——每步独立选择，反引号存在时不用 token 兜底；**命中**：等于某次调用
  的工具名，或是其主要参数的子串）≥ 一半能在证据中找到。证据优先取选出
  轨迹的工具调用；一组选出的轨迹都读不到时改与组内经验的执行动作（action
  段与正文）比对。提取不出候选命令（纯描述性步骤）视为通过。

**幂等**：草稿条目 id = `skill-` + sha256(sorted(组内经验 id) + cutoff)
[:24]；同组同批证据的草稿已在本空间时跳过该组——检查在读轨迹与 LLM
之前，不烧调用。

**SKILL.md 渲染**：YAML 头（name/description/tools，标量用 `json.dumps`
转义——JSON 双引号标量是合法 YAML，引号换行安全往返）+ `# {name}` 标题
（设计七项输出无标题字段，首版约定标题用 name）+ 四节（## 适用场景 /
## 前置条件 / ## 步骤 / ## 常见失败与处置）。

**草稿 MemoryUnit**：scope=输入条目所在空间（与来源经验同空间）、
tier=PROCEDURAL、tags=[draft]（manual）或 [trial]（auto 且附加条件满足）、
provenance=组内经验 id；`system_metadata` 记 memory_type/
skill_name/skill_version（prev+1 或 1）/category/capabilities/source_users/
source_trajectories/source_successes/trajectories_read/evidence_cutoff/
induction_batch（ISO 周号、周 2 位补零，如 2026w41）/strategy_version("0")/
author_principal("system:experience")/author_agent("")。

**auto 附加条件**（`skill_release=auto` 时）：trajectories_read ≥ 1、步骤
不命中危险操作清单、独立复现更多（user 口径作者 ≥ 3 或 trajectory 口径
轨迹 ≥ 5）——三者都满足才 [trial]，否则退回 [draft] 等人审。**危险操作
清单落 `construction/experience.py`**（`risky_operation_hit`，五模式：
disable-verification / recursive-delete / grant-all-permissions /
pipe-download-to-interpreter / credential-exfiltration）而非设计文档写的
scrub.py——现有经验部件是单文件模式（第一/二部分同款理由），且清单是确定性
规则、与轨迹清洗同域。

**运行摘要补写**：归纳结束扫本空间 `/memory/` 找 tags 含 run-summary 的
最新条目（按 t_ingest），正文 JSON 补 `skill_induction` = {groups, drafts,
experience_only}（experience_only = trajectories_read 为 0 的草稿数），
`kv.update` 原条目。组数口径 = 通过条件检查且 ≤ 20 截断后实际处理的组数。

**配置降级**：llm / embedder 任一 None、本空间未开启技能沉淀
（`curated_spaces` 中 `skill` 不为真）、入参空、混 scope → `abstract`
返回空，不炸轮次。但**经验 profile 显式引用 `abstractor: skill` 而部署
未配置 `globals.experience` 时装配 raise ValidationError**（与 appraiser
缺 author_key 拒绝同理：写了就是意图，缺配置是坏部署）。

**提示词内置**：归纳提示词是 `_build_prompt` 的内置中文模板，未像
merge / attribute 那样走 `prompts` 配置替换；需求出现时按现有 `PHASE_*`
机制接入，改动局部。

#### Curator 第 7 步

`_handle_skills` 插在 steps 元组的控规模（第 6 步）与类别重数（第 8 步）
之间，失败隔离同各步（异常捕获 → 记 failed_steps → 跳过）。技能条目不经
`units` 进来（`curate` 入口的 `_entry_filter` 按 `memory_type` 只放行经验
条目），本步自行扫描本空间的技能条目；本空间未开启技能沉淀时空过。四件事：

1. **聚合回写**：本空间的技能效用事件（`scan_events(scope)` 中
   `skill:` 键的归因事件）经 `aggregate_utility` 全量重算为统计记录写回
   本空间——**不带 prior、不做重定向**：技能没有合并与竞争组（第 4 步只
   整理经验条目），技能也豁免按作者清除（见下），无需带过非聚合产物
   字段。有帮助、有害（**加权次数**，与有害退休判定同口径）与加载次数
   （covered + neutral，归因事件总数）回写条目元数据 `skill_helpful` /
   `skill_harmful` / `skill_loads`，审核人 `get` 可见。聚合的作者限计窗
   对技能事件照常生效（技能条目不是单用户复现场景）。
2. **有害退休**：与经验同一规则（covered ≥ 5 且 Wilson 下界 > 0.5 →
   FORGOTTEN + 移出检索），作用于全部生效技能。不过滤 draft——未发布的
   技能没有加载途径、凑不满归因下限，统一规则不加标签分支。
3. **试用转正**：trial 且生效、归因满下限、validity ≥ 基线 → 标签改
   approved。**基线取本空间的类别基线**（技能的类别继承来源经验），无该
   类别时取 DEFAULT_BASELINE（与检索侧质量分同款兜底）。
4. **旧版取代**：同名已发布（approved/trial 标签）且生效的条目里版本号
   最大的为现行版，其余已发布旧版标记 SUPERSEDED + t_invalid（与 evolver
   的 SUPERSEDE 同款）。draft 不参与——待审新版不是「旧版」、也不算
   发布版；已退休的不参与（FORGOTTEN 已退出，无需再取代）。本轮退休的
   现行版不触发旧版回补。

落盘：计数回写、转正与取代改了条目本体 → `IndexBuilder.update(
FORWARD_ONLY)`（技能不经检索使用、正文索引无读取方）；退休走
`_forget` 的两条指令。**curator 改的是从 KV 读出的副本**，调用方闭包里
持有的原对象不受影响（测试经 RecordingIndexBuilder 的回写记录断言）。

**运行摘要记数**：skills_handled / skills_in_trial / skills_promoted /
skills_retired / skills_superseded / skills_events。`skills_in_trial` 是
**处理开始时的试用存量**口径——设计说「运行摘要记下本轮自动发布的条数」，
但新发布的试用在整理之后的归纳阶段才写入，第 7 步看到的是存量；精确的
「本轮新增」无从判定（无事件的条目没有统计记录，区分不了新发布与老试用）。
skills_superseded 是设计三项（自动发布/转正/退休）之外的补充计数。

#### Appraiser 技能归因

构造收 `experience: ExperienceConfig`（技能沉淀开关从 `curated_spaces`
取，无独立技能空间参数）与 `skill_tool_name`（加载技能的工具名，缺省
`skill_tool`，部署可配）。本空间未开启技能沉淀时识别与归因整段跳过。

- **识别**（`_identify_skills`）：轨迹工具调用里找 `skill_tool` 调用，
  **技能名取工具调用的主要参数**（`_primary_argument` 的第一个字符串值，
  agent-core 的 skill_tool 首参就是技能名）；在**经验落点空间**找同名
  **已发布**技能（approved/trial 标签且 ACTIVE），同名多版取版本号最大
  的现行版；没有同名发布版（agent 本地技能、被下线或驳回的）跳过不归因。
- **归因**：技能与取用过的经验**同一次**模型调用判定——listing 里经验用
  `[Entry {id}]`、技能用 `[Skill {key}]` 块；`retrieved` 为空但轨迹加载过
  技能时照常归因（技能不经检索返回，反馈只能从轨迹里来）。
- **事件**：写在**经验落点空间**、键 `skill:{技能名}:{版本号}`（条目标识
  改变时使用反馈不中断）；引用值取默认（技能不经检索、没有 usage_ref
  可带回）。

#### 按作者清除豁免（确认无代码改动）

设计的「不清除内容」含经审核技能——同空间后确认四点全部天然满足，无需
改清除路径：

1. 清除标记写在周期整理空间（`make_erasure_callback` 的 spaces 清单来自
   `curated_spaces`）；
2. 技能条目不进 curate units（`curate` 入口的 `_entry_filter` 按
   `memory_type` 只放行经验条目），「复现作者只剩被清除作者」的退休碰不
   到技能；
3. 技能条目本身不含作者信息，`delete_events_by_authors` 删的是被清除
   作者的效用事件（含其 `skill:` 归因事件——使用反馈随作者删除，下轮
   聚合全量重算自然消失），条目不动；
4. 技能统计每轮全量重算（第 7 步无 prior），没有需要封存的累计计数——
   sealed 字段天然不涉及。

#### 方案改版（技能同空间与复现口径）

初版落地后改版，四个变化：

1. **技能同空间**：初版技能落与经验空间配对的独立技能空间
   （`globals.experience_spaces` 条目的 `skill` 字段开空间）；改版后技能条目
   落来源经验所在空间，靠 `memory_type=skill` 隔离（类型隔离：对话
   与经验检索都取不到技能）。废除技能空间配对与 Appraiser/Abstractor
   的 `skill_space` 构造参数——识别、归因事件与草稿都落经验落点空间，
   开关从 `globals.experience` 的 `curated_spaces` 每空间 `skill` 字段取。
   Curator 第 7 步的技能空间复核（CONSOLIDATE 前对配对空间再鉴权）随之
   废除——经验空间的鉴权已覆盖同空间的技能读写。
2. **复现口径 per-space**：`curated_spaces` 每空间一项
   `repro_unit`（user / trajectory / auto，缺省 auto）。auto 按落点解析：
   经验落在写入者自己的空间（空间名由判定表 fallback 类别模板渲染，如
   `u_{user}` 渲出的 `u_alice`）按 trajectory、其余按 user；无判定表时
   按 `default_space` 兜底（own → trajectory）。
3. **装配期解析到底**：auto 是纯空间级判定（curated_spaces 是静态清单），
   装配期一次解析。`ExperienceConfig.resolved_repro_unit` 给出最终口径
   （形态匹配经 `SpaceNaming.is_fallback_space`——占位符段按「非空且不含
   路径分隔符」匹配，无占位符的常量模板退化为全等）；SkillAbstractor
   的 `_Round.repro_unit` 装最终口径，五条件与 auto 下限的判定处只认
   user / trajectory；Curator 收已解析的 `trajectory_spaces` 空间集，
   `_window_days_for` 对这些空间**关掉聚合的作者限计窗**（trajectory
   口径按轨迹数复现，单用户的连续复现轨迹被窗内去重会压到至多一条——
   两种计数法的限计错误代价不对称，误关只是轻度高估）。
4. **运行摘要条目标记**：Curator 写的运行摘要条目
   `system_metadata` 补 `memory_type=experience`——编辑者用 `list` 按
   `memory_type=experience` 与标签 `run-summary` 列出；条目的 tier 过滤
   （EPISODIC）先挡住它进下一轮整理，加标记不影响 curate 输入。

### 拒绝的方案

- **技能草稿走语义去重**：拒绝（见 dedup_products 契约）。同一技能新版与
  旧版内容天然相近，recall + LLM 判定会把新版判成重复丢弃；技能的版本
  演进靠条目 id 幂等（组内经验集合 + 证据截止时间的哈希）与版本号承载。
- **SKILL.md 标题独立输出字段**（模型返回第八项）：拒绝。七项输出是设计
  定稿的接口，标题与 name 恒同值（同步侧目录按 skill_name 命名），多一
  个字段只多一个不一致的机会。首版约定标题渲染 name。
- **YAML 标量手工转义**：拒绝。description 含引号换行时手写转义规则易漏；
  `json.dumps` 产出的双引号标量是合法 YAML（YAML 1.2 JSON 兼容子集），
  `yaml.safe_load` 安全往返。
- **危险操作清单落 `construction/experience/scrub.py`**（设计文档形态）：
  拒绝，同第二部分 erasure 的拒绝理由——经验部件是 `experience.py` 单文件
  模式，清单是五条确定性正则，拆包无收益。`risky_operation_hit` 与
  `tool_call_digest`（技能识别与命令覆盖共用）一并落在该文件。
- **第 7 步聚合带 prior**（与第 1 步同构）：拒绝。prior 的意义是带过
  merged_into / competition_group / sealed 三个非聚合产物字段，技能
  三者皆无（不合并、不竞争、豁免清除），全量重算即可，少一次逐条
  load_stats。
- **有害退休过滤 draft 标签**：拒绝。未发布技能没有加载途径、归因事件
  凑不满 5 次下限，过滤分支是死代码；统一规则少一个标签分支。
- **`skills_in_trial` 记本轮新增**：拒绝。第 7 步先于归纳执行，本轮新发布
  的试用此时尚未写入；「无统计记录 = 新发布」的判别不成立（老试用也可能
  无事件）。存量口径可复核、语义清楚。
- **技能归因单独一次模型调用**：拒绝。「与取用过的经验一起归因」是设计
  原文——同一次调用里模型看得到完整任务脉络（取用了什么、加载了什么、
  结果如何），单独调用要么重复给脉络、要么缺上下文。
- **auto 按事件作者或 memory_class 判定**（方案改版）：拒绝。效用事件的
  author 是 HMAC 作者摘要，不可逆渲染不回空间归属；own 直落条目写的
  `memory_class` 是部署经验类别，判不出写入者空间；运行时按作者分布统计
  则共享空间单用户使用会误判。空间名形态匹配 fallback 模板是纯空间级、
  装配期可判，与「写入落点或 fallback 空间」的字面语义对齐。
- **curator 运行时逐事件判口径**（方案改版）：拒绝。curated_spaces 是静态
  清单、auto 解析不依赖运行时数据，装配期一次解析到底（abstractor 装
  最终口径、curator 收空间集）省去每轮逐空间重复判定，也让判定处只认
  user / trajectory 两种值。

### 验证

- `tests/unit/construction/test_skill_abstractor.py`（52 用例）：
  dedup_products 契约（缺省 True / False 跳过去重直接落盘 / True 照常）；
  分组（类别分桶、capability 连边约束、大组贪心分片）；五条件（下限两种
  计数、独立复现排除、强证据、成功比例、prev 新证据、驳回后再复现、基线）；
  生成全链路（草稿全字段、幂等不烧 LLM、prev 版本 +1、空对象、非法 JSON
  重试、条件不过不调模型、max_groups 截断）；轨迹选取（每用户先各取一条、
  补足、上限）；校验（技能名规则、脱敏丢弃、命令覆盖命中/编造/兜底）；
  SKILL.md 渲染（形状、YAML 转义往返）；auto 附加条件（转正/危险操作退回/
  复现下限/轨迹计数/仅经验不发布）；运行摘要补写（含无摘要、experience_
  only 计数）；配置降级（无 llm / 无 embedder / 未开启 / 空入参）；
  TestAutoReproUnit（方案改版：auto + fallback 形态空间按 trajectory、
  auto + 共享空间按 user、无判定表按 default_space 兜底）。
- `tests/unit/construction/test_experience_curator.py`（62 用例）：
  TestSkillsStep（13 用例）：未开启空过；计数回写（加权口径、
  FORWARD_ONLY、统计落本空间）；转正（满下限/不足/低于基线）；有害退休
  （优先于转正）；旧版取代（同名现行版/draft 不参与/退休现行版不回补）；
  试用存量计数；curate 全链路；第 7 步失败隔离；stats 键格式。聚合限计
  （缺省窗内同作者只计一次 / trajectory 口径空间关限计）。运行摘要
  `memory_type=experience` 断言。
- `tests/unit/construction/test_experience.py`（87 用例）：
  TestResolvedReproUnit（显式口径原样 / auto + fallback 形态 / auto +
  共享空间 / 无表按 default_space / 清单外空间按 auto）与
  TestIsFallbackSpace（占位符模板形态匹配 / 常量模板全等 / 无
  fallback_class 恒不命中）。
- `tests/unit/construction/test_experience_appraiser.py` TestSkillAttribution
  （7 用例）+ TestAttribution 补一例：识别归因（事件写经验落点空间）；
  技能与经验同一次调用；draft 跳过 / trial 算已发布；多版取现行；
  无同名发布版跳过；自定义工具名；未开启时 skill_tool 调用不触发归因。

---

## 第四部分：判定更新链

写入链路交付了判定记录的**更新**：补报 `external_score` 与下一条轨迹的
用户反馈都按证据等级更新判定（`revision` 递增）。但更新后的**补做**没有做：
判定结论事后翻转时，已写入的复现事件与归因事件还挂着旧结论——效用统计
失真，检索质量分与升为技能的条件检查（`strong_success` 只认
`external` / `user_feedback` 级的成功）跟着错。

写入链路在四处留了钩子：`_apply_external_supplement` 与
`_backfill_previous_verdict` 更新判定后直接返回；finalize 把条目 id 记入
判定记录 `derived_ids`（注释写明「判定更新时按新结论为这些经验补写复现
事件」）；`ExecutionTrajectoryRecord` / `VerdictRecord` 的 docstring 标注
读取方「（判定更新链）」。判定更新链把这些钩子接上。

前提已在写入链路铺好：效用事件键含判定修订号
（`/experience/utility/{条目 id}/{attr|repro}:{作者摘要}:{trajectory_id}:{revision}`），
`aggregate_utility` 聚合时**同 (条目, 类型, 作者, 轨迹) 只取修订号最大的一条**
——补写新修订号的事件即可，聚合侧零改动。补报与反馈依赖接入方行为
（`external_score` 补报、`previous_trajectory_id` 关联），openjiwen 仓轨迹
传出落地前线上无补做流量。

### 决策

1. **补做随判定更新同步执行**。两个判定更新入口（补报
   `_apply_external_supplement`、反馈回填 `_backfill_previous_verdict`）在
   判定保存之后调用新方法 `_post_verdict_update`，同一请求内完成，调用方
   无感知。判定记录与轨迹记录此刻都在手（补报的 `existing`、回填的
   `previous`），同步做代价最小。

2. **补写复现事件：判定更新即补写，不要求结论翻转**。`derived_ids` 逐条按
   新判定（`outcome` / `evidence` / 新 `revision`）写复现事件，键含新修订
   号、insert-only 幂等。证据等级单独升级（结论未翻转，如 model_only
   success 被补报升为 external success）也补写——升为技能只认
   external / user_feedback 级的成功，`evidence` 变化影响条件检查。

3. **重新归因：结论翻转才做**。`old_outcome != verdict.outcome` 时复用第 5
   步 `_attribute`（含技能识别）：归因模型的主要输入是任务结论，结论没变
   重跑只是浪费一次调用。返回值丢弃（正常流程里归因结论传抽取算子，补做
   场景只要副作用）——新修订号的归因事件。归因事件 `evidence` 字段随新
   判定写入，聚合加权随之修正。

4. **轨迹记录不在时整体跳过**。两类补写事件的内容字段都来自轨迹记录
   （复现事件的 `source_scope` / `retrieved_ids`，归因的消息与使用记录）。
   反馈路径上记录不在时判定更新本身也不会发生（判定反馈的输入要上一条
   的最终回复）；防御分支覆盖回填前后两次读取之间记录恰好过期的竞态。

5. **补做失败只记 warning**。判定已更新是主链路成果；补做（效用存储故障、
   重新归因模型失败、超时限）不把补报/反馈请求报失败。统计修正由下一次
   更高等级证据的更新再触发（新修订号新键）；同修订号的补写幂等，但该
   修订号的重新归因缺失要等判定再次更新才补——接受。

6. **已产出的条目不撤回**（设计立场）：判定翻转只修正效用记录，
   不删条目；条目的去留由它们自身的使用效果决定（与 dedup_decisions
   「宁多勿丢」同一哲学）。

7. **经验空间取部署配置**（`self._experience_space`），与 finalize 无落盘
   条目时的回退分支一致。首版全局一个经验空间。

8. **补报须同一生产者**：外部结果决定证据等级最高的结论，只
   接受**发送这条轨迹的接入方**补报——生产者主体从轨迹请求的内核保留键
   （接口层从认证结果注入，见第一部分决策八）与 `scope.org` 组出，与轨迹记录
   落盘的 `producer` 按主体维（org/user/agent）逐维比对（`same_dims`），
   不同生产者抛 `PermissionDeniedError`、判定保持原样。调用方自报的接入方
   标识在写入边界已被保留键校验拒绝，这里核对的是认证主体。

9. **反馈回填按会话核对**：回填只对**同一会话**的上一条轨迹
   ——轨迹记录归属主体的 session 维相同，且本条请求 context 带 `turn` 时
   上一条的 turn 须更小（同会话内回合递增，等于或倒序说明关联错了轨迹）；
   上一条无 turn 时无从比对、跳过回填。核对只 gate 回填（关联错则不回填，
   Pair P 配对判定照常执行），不 gate 补报——补报有生产者核对把门。

### 拒绝的方案

1. **异步补做 / 下轮周期整理时按修订号差补**：统计修正延迟一轮（最长
   7 天）；判定更新的调用点记录都在手，同步做代价最小；失败隔离已保证
   不拖垮主链路。
2. **原位改写旧事件（revision 0 的 outcome 直接更新）**：违背效用事件
   insert-only 契约（`_append_event` 的幂等靠键不存在），且丢历史——事件
   只追加是审计与回放的前提。
3. **证据升级也重新归因**：outcome 没变时归因结论大概率不变，多一次模型
   调用换不回结论变化；接受旧归因事件 `evidence` 加权的小偏差（聚合仍按
   最新修订号取归因结论，只是该结论的 evidence 停在旧等级）。
4. **轨迹记录不在时按判定记录部分补写**（`retrieved_ids` 写空）：
   复现事件的复现计数只计「使用记录中不含该条目及同组条目」的轨迹，
   `retrieved_ids` 写空会被当成无使用记录的轨迹计入复现——语义错误，
   跳过更安全。
5. **为补做单设归因流程/prompt**：第 5 步的输入就是「轨迹消息 + 判定 +
   条目内容」，新判定传入即得新归因；复用避免两处实现漂移。

### 验证

- `tests/unit/construction/test_experience_appraiser.py` 新增
  `TestVerdictUpdateChain` 6 例：补报翻转补写复现事件（内容与幂等）、
  补报翻转重新归因（derived_ids 空不补复现）、反馈升级证据不翻转只补
  复现不重归因（LLM 调用数锁定）、反馈翻转对上一条重新归因、轨迹记录
  不在跳过补做（防御分支直测）、补做失败不拖垮判定更新。
- 补报生产者核对 2 例：其他 agent / 其他 user 的补报抛
  `PermissionDeniedError` 且判定不变。
- 会话核对 4 例：跨会话（session 维不同）不回填、turn 不递增不
  回填、上一条无 turn 不回填、同会话 turn 递增正常回填（Pair P 照常）。
- ScriptedLLM 队列按真实调用序编排：补做的重新归因插在第 3 步（反馈）
  与第 4 步（成败判定）之间——调用次数本身是被测行为。

---

## 装配守卫（全特性收尾）

- `examples/config_experience.yml` 的装配测试
  （`tests/unit/api/test_experience_examples_config.py`）覆盖经验 profile
  的真实装配路径（双 profile / 各算子 / dedup_decisions 限定全套形态）。
- 守卫落地时发现并修复两处工厂延迟 import 路径错误：curator 与 skill
  abstractor 的 `EmbedderProducer` / `LlmProducer` 误写包根路径
  （应为 `.base`）——此前无任何过测装配路径走到，任何真实部署装配经验
  profile 都会在启动时 ImportError。
- `_parse_dedup_decisions` 的合法路径由该守卫间接触达（`EvolverProducer`
  完整装配依赖链在单测环境搭不起来，非法形态的专项失败断言待补）。
- 按作者清除的两处装配期校验（`assembly.py`，用例在
  `tests/unit/api/test_experience_space_authorization.py`）：KV 同源——
  curator / appraiser 显式 `message_store` 引用与标记 store 非同一实例
  即拒绝；author_key 多实例一致性——评价算子密钥不一致即拒绝。
