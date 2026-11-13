# F11 — Evolver 阶段编排与扩展

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-05 |
| 影响范围 | `construction/evolver_impl/`、`construction/AGENTS.md`、`docs/specs/S05-construction.md` |
| 测试基线 | `pytest tests/unit -m unit`：5883 通过、7 跳过；修改的 Python 文件 Ruff 通过 |

## 背景

经验写入引入评价与收尾、周期整理引入 Curator 后，动态抽取主方法仍把真正的阶段藏在
`_dynamic_extract` 与 `_extract_step` 中。读者看见 extract → consolidate → reflect，
却看不到来源约束、归属路由与分层发生在哪里。普通 CONSOLIDATE 的整理、归纳和产物
落盘也需要以相同方式展示，便于开发者扩展并核对算子接线。

## 决策

1. 保留 OrchestratingEvolver / DynamicEvolver 的继承和具名配置机制。每条流程在各自
   主方法列清阶段，开发者通过覆盖阶段方法或主方法扩展，调用方仍选择配置好的完整流程。
2. EXTRACT 使用每次调用独立的 `ExtractState`，保存 sources、procedural、appraisal、
   context 和 candidates；不把请求阶段状态放在共享 Evolver 实例上。
3. 抽取阶段只调用 Extractor。Snapshot 继承与 prompt 传播放在候选准备阶段；路由与分层
   在主方法显式调用。路由先于去重，确保召回在最终目标空间进行。
4. legacy 保留 `dedup_extract` 的直写开关与 `_dedup_batch` 的批量判定和有序应用动作。
   dynamic 保持只判定 → 原地反思 → 执行决策，并复用同源真源复核、批内后继重判与落盘。
5. procedural 保留父类路径：不维护原文、不收集上下文、不继承 Snapshot、不去重，
   不增加 dynamic 反思 prompt 传播；候选经路由与分层后直接写入。
6. CONSOLIDATE 展示可选整理 → 归纳 → 产物准备 → 分层 → 按产物契约持久化。
   `Abstractor.dedup_products=False` 仍直接写入，避免把技能新版误判为重复。
7. 补齐 DynamicEvolver 装配时的 Curator 依赖：其可选装配规则与父类相同，显式引用
   使用具名共享实例，不再出现配置存在而运行时遗漏整理算子的情况。
8. `EXPERIENCE_CONSOLIDATE` 在任何整理副作用前校验经验类型与 Curator，随后复用一次
   整理归纳流程；专用 mode 的调度、筛选与持久化恢复由 control/API 负责。

EXTRACT 流程：

```text
appraise → context → extract → prepare candidates → route → annotate
  ├─ legacy：按配置直接落盘或执行判定与落盘
  ├─ dynamic：判定 → 反思 → 落盘
  └─ procedural：直接落盘
→ finalize
```

评价未装配则跳过。评价抛异常或拒绝准入时不收尾；后续阶段抛出 Exception 时以 None
收尾并保留原异常；正常空结果仍收尾；finalize 自身错误只记录。

## 拒绝的方案

- 请求传任意阶段列表：算子组合有接口与阶段契约，不能由单次请求任意拼接。
- 为整理主流程另建一套算子 ABC 或总编排器：现有继承与阶段方法足够，避免增加依赖层。
- 把 legacy 全部决策与全部写入强行拆开：原批量路径的动作顺序可能影响后续候选，
  这属于行为修改，超出本次编排整理范围。
- 强制所有经验抽取器声明 requires_appraisal 并在装配时失败：本次不增加这一规则，
  当前 ExperienceExtractor 无评价时仍按原实现处理，不宣称已支持无评价经验抽取。
- 一次 add 串行运行 EXTRACT 与 CONSOLIDATE：抽取本次输入与后台整理已有经验是独立任务。

## 验证

- 普通与动态抽取均验证：候选准备先于路由，路由先于分层，去重与写入接收最终候选。
- 上下文、候选准备、路由、持久化阶段失败：同一个异常继续向上传播，只以 None 收尾一次。
- procedural 跳过上下文和去重，只评价/收尾一次，保持原 prompt 传播行为。
- 重用同一 Evolver 的多次调用各自持有独立状态、上下文与候选集合。
- CONSOLIDATE 只归纳 Curator 保留的条目，按产物规则选择直写或去重写入。
- 具名 DynamicEvolver 真实装配使用同一具名 Curator；经验专用 mode 拒绝缺 Curator
  与混入普通类型，正常执行不重复整理。

## 已知遗留

- reflect 仍为默认 no-op，只支持对决策引用的候选原地修正；没有扩展替换候选对象语义。
- 各存储端口之间不增加事务、回滚或断点恢复机制；阶段失败可能已产生原有部分副作用。
- ExperienceExtractor 当前仍依赖评价上下文，解耦轨迹准备与价值评价需要另行设计。
