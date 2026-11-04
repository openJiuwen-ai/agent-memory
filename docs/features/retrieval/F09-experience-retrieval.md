# F09 — 经验记忆检索

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-19 |
| 影响范围 | `jiuwen_memory/common/experience.py`（新）、`jiuwen_memory/retrieval/`（base / types / bootstrap / selector.py 新 / selector_impl/ 新 / retriever_impl/pipeline_retriever.py）、`jiuwen_memory/control/`（pipeline / engine / pipeline_impl/metadata_pipeline / engine_impl/in_memory_engine / engine_impl/cloud_engine）、`jiuwen_memory/api/memory_api_impl/query_ops.py` |
| 测试基线 | 局部回归：`tests/unit/retrieval` + `tests/unit/common` + `tests/unit/construction` 1269 通过、`tests/unit/api` 327 通过；`tests/unit/api` + `tests/unit/control` 637 通过；新增 45 用例（另迁移 33 用例至 common）、全过；ruff 涉改文件干净 |

## 背景

[F10](../construction/F10-experience-memory-and-skill.md)（写入链路）交付了
轨迹 → 评价 → 提炼 → 经验条目落盘的写入闭环。本文交付
**经验记忆检索**：经验空间的 `search` 走专用的 pipeline profile，检索链路在
相关性过滤与截断之间增加**选取阶段**（Selector），按四件事
决定返回哪几条经验、按什么顺序。内容分三块：

```
共用部件下沉：效用域部件（效用事件/统计记录/UtilityStore/策略取值/
        质量分/usage_ref）从 construction/experience.py 拆到 common/experience.py
Selector 算子：接口 + ExperienceSelector 实现 + PipelineRetriever 接线
空间策略路由：query_ops 消费 SpacePolicy.pipeline_profiles
```

agent-core 侧（openjiuwen 仓库）不做；deploy 配置不含经验 profile。

## 目标

1. `PipelineRetriever` 支持可选选取阶段：配置 `selector_enabled` 时启用，
   未配置时链路行为与旧版逐位相同（对话记忆检索零影响）。
2. 选取算法落地：复现要求、排序分（相关性^α × 质量分^β）、探索名额、
   按能力分组；公式与常量单点维护在 `common/experience.py`。
3. `SpacePolicy.pipeline_profiles` 的 `"*"` 键在检索侧生效：单空间 search 绑定
   profile、跨空间检索排除绑定空间。
4. 检索层不 import 构建层（retrieval → construction 严禁），效用部件因此
   下沉 common——两侧共用、依赖方向不破坏。

## 非目标

- 周期整理 Curator（统计记录与类别基线的周期聚合写入——检索侧只读，见决策五）、
  技能归纳 SkillAbstractor、判定更新链——三者归档在
  [F10](../construction/F10-experience-memory-and-skill.md)。
- `pipeline_profiles` 的非 `"*"` 键（按记忆类型细分绑定）：首版只认 `"*"`。
- agent-core 侧（openjiuwen 仓）的使用记录回报（消费 usage_ref）与经验条目
  呈现；本仓 adapter 已交付 provider 侧轨迹转交与技能同步（见
  [F10](../construction/F10-experience-memory-and-skill.md) 非目标）。

---

## 决策

### 一、效用部件下沉 `common/experience.py`

写入链路（F09）把全部共用部件放在 `construction/experience.py`（F09 决策三）。
本文的
Selector 也要读效用记录（统计、基线、复现作者）并复用质量分公式，而检索层
不得 import 构建层（S04 边界；`tests/unit/retrieval/test_experience_layer_boundary.py`
源码扫描 + 运行时验证双重固定）。因此把**效用域**部件拆出：

- `common/experience.py`（新）：键常量（`/experience/` 统一前缀——
  `/experience/utility/`、`/experience/stats/`、
  `/experience/repro-authors/`、`/experience/counts/`、
  `/experience/baseline`、`/experience/erased-authors/`；经验条目可与对话记忆同空间、专用键收在 `/experience/` 段下）、`AttributionEvent` /
  `ReproductionEvent` / `StatsRecord`、`UtilityStore`（读写全部效用记录）、
  策略取值（`EVIDENCE_WEIGHTS`、`SMOOTHING_K=2.0`、质量与排序权重、
  `RANK_ALPHA=2.0`、`RANK_BETA=1.0`、`EXPLORATION_THRESHOLD=5`、
  `EXPLORATION_QUOTA=1`、`ISOLATION_PERIOD_DAYS=7`、`DEFAULT_BASELINE=0.5`、
  `REPRO_AUTHOR_LIMIT=2`）、质量分（`weighted_count` / `validity` /
  `freshness` / `quality_score`）、`usage_ref` 编解码。
- `construction/experience.py`（既有）：保留构建侧专用部件（轨迹/判定记录、
  报错规则、轨迹消息、密钥清洗与脱敏、作者摘要、`ExperienceAppraisal`），
  效用域从 common 引入。

分家线是**读者**：效用域记录的读取方横跨构建（写入）与检索（选取），轨迹/
判定记录的读取方只在构建侧（周期整理 / 技能归纳 / 判定更新链）。`days_since` 从模块私有改公共
（selector 的隔离期计算要用）。

### 二、Selector 是独立可选算子（`retrieval/selector.py`）

形式与 Fuser/Discloser 相同：契约（`Selector` + `SelectorProducer`）在顶层
接口文件，实现（`selector_impl/`）以 `@SelectorProducer.register("<名>")`
自注册，`retrieval/bootstrap.py` 统一触发。`RetrievalOperatorType` 新增
`SELECTOR`（列在 FUSER 之后）。

```python
class Selector(RetrievalOperator):
    def select(self, survivors: list[ScoredMemoryUnit], query: RetrievalQuery) -> list[ScoredMemoryUnit]: ...
```

与 Fuser 的分工：Fuser 把多路召回的候选合并成一份按相关性排好的列表；
Selector 在**过滤后**的候选上做名额分配——一次返回里探索名额至多一条、每种
能力先各出一条。`select` 不带 scope 参数：scope 是召回前置谓词，一次检索的
候选同属一个空间，取 `survivors[0].unit.scope` 即可（经验空间条目同 scope）。

### 三、PipelineRetriever 接线：[8.5] 阈值过滤后、top_k 截断前

```
[8] apply_threshold（相关性阈值 + min_results 兜底回填）
[8.5] selector.select(survivors, query)   # 未配置时跳过，行为不变
      units = {c.unit_id: c.unit for c in survivors}   # 副本后重建条目字典
      step("select", t0, n=len(survivors))             # 轨迹阶段名 "select"
[9] 截断 top_k
```

三个实现要点：

- **位置**：必须在阈值后（零分/低分候选不进选取，探索名额只在有意义的候选
  中挑）、top_k 前（选取决定顺序，截断是最后一步）。
- **副本后重建 units**：selector 不原地修改候选、会换上带引用值的条目副本，
  披露读的条目字典必须随之重建，副本才能进返回结果（`test_pipeline_retriever_selector.py`
  用打标记 selector + 记录型 discloser 固定）。
- **构造参数 keyword-only**：`PipelineRetriever(..., *, selector: Selector | None =
  None)`——构造参数已多，位置传参易错位。`_build` 装配经
  `config.get("selector_enabled", False)` 判开关后
  `SelectorProducer.dep(config, default="experience")`：true 而未指名时取缺省
  实现 experience，与 `rerank_enabled` 同构。`Factory.dep` 不支持返回 None，
  调用方先判开关再取依赖。

### 四、ExperienceSelector 四步算法（`selector_impl/experience_selector.py`）

```
第 0 关  复现要求：executable is True 的条目须统计记录 authors ≥ 2 或
        复现作者记录 ≥ 2（REPRO_AUTHOR_LIMIT）；环境事实/不可执行不设关。
        统计记录优先（周期聚合），没有或未满时读沉淀时增量维护的复现作者
        记录——第二个用户的轨迹复现后随即放行，不等周期聚合（风险控制）。
第 1 步  探索名额：covered（归因次数）< EXPLORATION_THRESHOLD(5) 的候选算
        未经验证，其中排序分最高的一条先入选，ref="0.e"。
第 2 步  按能力分组：组键 (category, capability_or_"")；隔离期内（落盘不满
        ISOLATION_PERIOD_DAYS=7 天）的条目排除；各组最优进 grouped（组间按
        组最优排序分降序），组内其余进 fill。
第 3 步  补满：fill 按 rank 降序排在尾部。
返回    [explore_pick?] + grouped + fill，每条换成带 usage_ref 的副本。
```

排序分 = 相关性^RANK_ALPHA(2) × 质量分^RANK_BETA(1)；相关性 = 检索分 ÷
本次参与排序的候选（已过复现要求）的最高检索分；质量分 = 0.7 × 有效性 +
0.3 × 新鲜度（公式在 `common/experience.py`，本模块只组合）。

### 五、五个关键取值决策

1. **`DEFAULT_BASELINE=0.5`（冷启动中性基线）**：统计记录与类别基线由周期整理
   Curator（F10）的聚合步骤写入，检索侧只读。两样都没有时（冷启动）有效性取中性值
   0.5——新条目不因「还没有统计」被排到队尾，也不因无据而虚高；探索名额
   机制会把它标记为未经验证先行试探。
2. **`score` 保持检索分，不掺质量分**：返回给 agent 的分数仍是「与检索句的
   匹配程度」；质量分只在排序时用。调用方（agent-core）按 score 决定取用
   深度时不应被质量分污染。
3. **`t_ingest` 缺失按已出隔离期处理**：落盘时间是隔离期的判据，元数据缺失
   时若按「在隔离期内」处理会把条目永久锁死在探索名额之外；按已出隔离期
   （`_in_isolation` 返回 False）让条目正常参与分组。
4. **`max_score ≤ 0` 时相关性取 1**：全零分候选（如纯关键词通道无校准）参与
   排序时，排序退化为按质量分——除零防护的同时保留确定性顺序。
5. **探索名额不受隔离期限制，第 2/3 步排除**：隔离期是「新条目未经时间检验
   不进主力位」的约束，探索名额本身就是给未经验证条目的试探位——两者叠加
   会让新条目在隔离期内完全不可见。环境事实不带 capability，组键 capability
   取空串自成一组，保住「有环境事实通过过滤时至少返回 1 条」的保证。

### 六、副本语义与 metadata 裁剪

`select` 返回的每个候选换成条目副本：`replace(candidate.unit,
system_metadata=_stripped_metadata + usage_ref)` 再 `replace(candidate,
unit=...)`。`system_metadata` 只留 `category` 与三段（`condition` / 
`capability` / `action`，只有条件动作带）加 `usage_ref`；写入侧落盘的
memory_type / pipeline / 抽取策略 / 作者标记 / 可执行标记等内部元数据不
外发。不原地修改入参：存储后端返回的可能是缓存中的同一对象。`usage_ref`
（版本 "0" + 探索位 "e"/"n"）是 agent-core 回报使用记录的引用值。

### 七、空间策略路由（`api/memory_api_impl/query_ops.py`）

`SpacePolicy.pipeline_profiles` 的 `"*"` 键把空间绑定到某个 pipeline
profile（写入链路已定义字段，本文消费）：

- **单空间 search**：目标空间有绑定值时写进 `options[pipeline.route_key]`
  （`MetadataPipeline` 缺省 "memory_type"），**覆盖调用方自带的同键值**——
  绑定是空间级决定，不因请求而变。写入点在 `_recall_permission_context`
  之前：鉴权与执行按同一个 profile（鉴权读到的路由值与实际执行的链路一致，
  不会「按 A profile 鉴权、走 B profile 执行」）。绑定的取值没有对应 profile
  （引擎未注入 pipeline 或 `has_profile_for` 为假）时抛 `ValidationError`：
  静默回落默认 profile 会让经验空间走对话记忆的 retriever，部署错配应在入口
  暴露而不是产生错误结果。
- **跨空间检索排除绑定空间**（四理由：经验选取的名额规则在跨空间轮转
  合并与截断下无法成立、两种分数不可比、混在对话结果里的经验得不到使用记录
  无从归因）：显式 `spaces` 点名即 `ValidationError`（调用方自己的入参，错误
  信息回显空间名与绑定值）；主体反查（`spaces` 为空）静默排除——反查给的是
  超集契约，排除不改变「不遗漏可读的普通空间」的承诺，全部候选被排除时返回
  合法空结果。
- **绑定读取容错**（`_bound_route_value`）：空间不存在或后端不可用
  （BackendError/NotFoundError）按无绑定处理——空间可达性由鉴权裁决，路由
  侧不重复；scope 缺 org/space 维（非空间目标）同样无绑定。只认 `"*"` 键，
  strip 后为空返回 None。

### 八、两个新抽象 property（`MemoryPipeline.route_key` / `MemoryEngine.pipeline`）

API 层要读路由键名（写入 `options[route_key]`）并校验绑定值可达
（`has_profile_for`），这两个能力原本藏在实现里。新增：

- `MemoryPipeline.route_key`（抽象 property）：本 pipeline 的路由键名。
- `MemoryEngine.pipeline`（抽象 property）：注入的 pipeline（未注入为 None），
  docstring 明示供 API 层校验空间绑定 profile 的可达性。

所有测试替身须实现（`test_cloud_engine.py` 的 `_MessageTypePipeline` 补
`route_key` 返回 "message_type" 即为此）。

## 拒绝的方案

- **Selector 带显式 scope 参数**：被拒。scope 是召回前置谓词，进入选取的候选
  必然同 scope；为恒真约束增加接口参数只会让误传更难发现。
- **质量分写进返回的 score**：被拒（决策五第 2 条），score 语义是检索匹配度。
- **`Factory.dep` 支持「缺省返回 None」**：被拒。dep 的既有语义是「缺省 → default
  匿名实例，无 default 抛错」，加 None 分支会让所有消费方的判空义务消失、
  装配错误延后到运行期；调用方（`_build`）先判 `selector_enabled` 开关再取依赖。
- **选取阶段放在 top_k 截断之后**：被拒。截断后 selector 看不到完整候选，
  探索名额与按能力分组都会在被截断的集合上做出错误选择。
- **绑定空间的跨空间检索静默全拒（含反查）**：被拒。反查是超集契约，
  排除绑定空间后普通空间照常可搜；全部拒绝对调用方而言把「可读空间变少」
  误报成「无权」。
- **绑定值无 profile 时静默回落默认 profile**：被拒（决策七），部署错配必须
  在入口报错。
- **效用部件留在 construction、retrieval 经参数注入**：被拒。注入会让
  pipeline 装配多一条隐性依赖链（retriever 的 builder 要从 construction
  拿对象），且质量分公式两侧各写一份必然分叉；下沉 common 一处维护。

## 验证

`PYTHONIOENCODING=utf-8 python -m pytest <路径> -m unit --no-header`；全量
交付时按用户指示只跑局部目录回归：

- `tests/unit/retrieval/test_experience_selector.py`（新，30 用例）：复现
  要求 7（executable 门、统计/即时两路计数）、排序 4（相关性加权、质量分
  破平、baseline 回退、全零分退化）、探索名额 4、按能力分组 4、隔离期 4
  （7 天边界、t_ingest=None 不隔离、naive 时间补 UTC）、副本语义 4（score
  保持、metadata 裁剪、不改入参、usage_ref 探索位）、边界 3。
- `tests/unit/retrieval/test_pipeline_retriever_selector.py`（新，5 用例）：
  零分候选不进选取、副本到达披露与结果、top_k 截断在 select 之后、无
  selector 行为不变、轨迹 select 阶段有无。
- `tests/unit/retrieval/test_experience_layer_boundary.py`（新，2 用例）：源码扫描
  （rglob 检查 "jiuwen_memory.construction" 不出现）+ 运行时（import
  experience_selector 前后 sys.modules 的 construction 集合差为空）。
- `tests/unit/common/test_experience_utility.py`（新，33 用例）：
  `construction/test_experience.py` 的效用记录节随源码迁移（键布局、UtilityStore
  往返/幂等/上限、质量分各函数、usage_ref 编解码）；轨迹/判定/清洗等构建侧
  用例留在原文件。
- `tests/unit/api/test_experience_search_routing.py`（新，8 用例）：真实装配
  端到端（cloud 引擎 + 空间感知判定 + MetadataPipeline 双 profile + 探针
  retriever）——绑定路由、覆盖调用方值、普通空间不走绑定、无 pipeline /
  无 profile 两路参数错误、跨空间显式拒绝、反查排除、反查全绑定空结果。
- `tests/unit/control/test_cloud_engine.py`（改）：替身补 `route_key`。
- 局部回归：retrieval+common+construction 1269 通过、api 327 通过；
  api+control 637 通过。ruff 涉改文件零告警。
