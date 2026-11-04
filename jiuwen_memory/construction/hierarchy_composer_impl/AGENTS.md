# TIME 构建实现

规约：[S05-construction.md](../../../docs/specs/S05-construction.md)。

## 模块地图

- time_pipeline / scene_pipeline / event_pipeline：各层确定性分组和参数校验。
- grouping_support：语义表示、向量合法性与相邻相似度计算。
- parent_enrichment：结构摘录、完整事实汇集、模型摘要及父层标注。
- time_hierarchy_pipeline：逐层完成摘要，再将父节点输入上层分组。
- incremental_pipeline：相邻单层分组、静默封口、安全前缀与下层待定边界。
- profile_config：不可变构建 profile 的配置解析。
- default_composer：候选树校验、持久化与区间替换，不负责候选扫描。

## 行为铁律

- 输入先复制，叶正文、来源、时间和生命周期保持，仅变更结构引用。
- 完整事实由子事实向上传递，不能从摘要反推，摘录限额不能截断事实或模型输入。
- 原始输入和候选树验证通过后才持久化。新父、叶父边、旧父归档和索引按既定顺序写入。
- 所有写入经 IndexBuilder；部分失败返回真实完成项和 repair_required，不宣称回滚。
- 内部增量传入准入回调时，准备结束及每批持久化前必须调用；只执行外部裁决，
  不读取身份或自行实现权限。准备拒绝不写入，写入期间拒绝保留部分结果与修复要求。
- 增量准备段只构造副本、分组及验证，无写入失败通过 HierarchyPreparationError 传递；
  _persist 必须位于准备异常转换边界之外，不能把部分写入标记为可安全重试。
- 只支持 TIME 父链前缀；显式重建须备齐完整旧子树，禁止局部截断后提交。

## 与其他子目录的边界

控制层提供授权范围内的完整输入并调度；Evolver 只委托 Composer；公共类型负责
纯结构校验，IndexBuilder 交付本体和检索索引。这里不读取策略开关、不实现权限、
后台扫描、树查询或自动修复；静默封口是传入时刻驱动的纯构建规则。
