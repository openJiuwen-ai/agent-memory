# F06 — filters 契约治理落地规范（团队执行版）

- 日期：2026-09-14
- 状态：生效（作为评审纪律；CI 强制项见 §5，落地时机 = 第一次真实的 L1 变更提案）
- 上游：[F05-filter-contract-governance.md](F05-filter-contract-governance.md)（方案与动机）
- 适用对象：所有改动 `jiuwen_memory/common/type_def/filter.py`、`memory_filter.py`、
  四个后端编译器（`_pg.py` / `elasticsearch_fulltext.py` / `milvus_vector.py` /
  `in_memory_fusion_store.py`）或新增 filters 消费方（检索/演进/未来）的提交人。

## 1. 触发条件：什么时候必须走本规范

| 改动 | 是否走本规范 |
|---|---|
| 只用现有 9 算子 + 现有字段/命名空间**造句**（构造 FilterExpr） | 否——造句自由，零流程 |
| 往 `FilterOp` / `FilterLogic` 枚举**加新成员** | 是——军规一 + 二 + 三全套 |
| 改任何**已有算子的求值/编译语义**（含"顺手修"） | **禁止**——走新算子，不修旧词 |
| 往 `_BUILTIN_FIELDS` 加新内置字段 | **禁止**——见 §6 冻结令 |
| 改任一后端编译器对既有算子的翻译 | 是——军规二 + 三（视为 L1 级变更） |
| 改字段哨兵 / 索引投影约定（array_marker、T_EVENT_UNKNOWN…） | 是——最高警戒，"三处同改"清单见 §7 |
| 新增 filters 消费方（新演进 mode 选料、新检索通道） | 仅过 §3 决策树，不动共享层则到此为止 |

## 2. 军规一：纯增量（PR 检查单）

改动 `_BUILTIN_FIELDS` / `FilterOp` 的 PR，逐项自查：

- [ ] 没有任何既有枚举值被**删除、改名、改语义**（diff 里旧成员只允许出现在注释/文档）
- [ ] 没有往 `_BUILTIN_FIELDS` 加字段（冻结令，§6）
- [ ] 新算子的语义与所有既有算子**不重叠**（若可用既有算子组合表达，先回答"为什么不组合"）
- [ ] 语义文档写进算子枚举的行内注释（像现有 `CONTAINS` 那样写清楚边界）

## 3. 四级决策树：新需求的标准路径

任何"我想筛 XX 但 filters 表达不了"的需求（**含新演进算法选料**），按下表顺序走，
**能停在低层就不上高层**：

```
需求进来
  │
  ├─ L0 词汇够？  现有 9 算子 + 命名空间能组合 → 直接造句，结束
  │     例：lifecycle IN [active] / t_message GTE cutoff / middle NE "true"
  │
  ├─ L1 该进核心？  检索也会用到的通用谓词（如 exists）
  │     → 走 §2 军规一 + §4 表态矩阵 + §5 双消费方回归
  │     例：字段存在性判断（既筛演进候选，检索也筛）
  │
  ├─ L2 后置够？  消费方专属 + 不需要下推（量小/点读路径）
  │     → 消费方内部 Python 后置筛选，不进契约、不进持久化注册表
  │     例：dreaming 某模式要"内容长度 < N"——resolver 内后置
  │
  └─ L3 打标够？  消费方专属 + 量大需索引级过滤
        → 构建期往 user_metadata.* 打标，读侧用现成 eq/in 查命名空间
        例：按"已参与过某轮 dream"筛 → 打 dream_batch 标
```

判据速查：

| 问题 | 答案 → 层级 |
|---|---|
| 检索将来会不会也要这个条件？ | 会 → **L1** |
| 数据量小、捞回来再筛不心疼？ | 是 → **L2** |
| 条件本质是"这类记忆被系统处理过"？ | 是 → **L3 打标** |

## 4. 军规二：表态矩阵（加新算子的硬性交付物）

新算子 PR 必须附带**算子 × 后端矩阵行**，四个翻译官逐一表态（会翻 / 显式报错），
禁止"静默不支持"：

| 算子 | pg | ES | Milvus | Python 求值器 |
|---|---|---|---|---|
| （示例）contains | ✅ jsonb OR | ✅ term+array_marker | ✅ json_contains | ✅ 成员判断 |
| `<新算子>` | PR 时填 | PR 时填 | PR 时填 | PR 时填 |

- **显式报错是合法表态**（正面案例：Milvus 对 eq/ne 抛 ValidationError）——
  报错优于静默漏翻。
- 四个编译入口都是**纯函数**（`compile_pg_filter` / `_filter_clause` /
  `_filter_clause` / `matches_memory_unit`），不需要起任何真后端即可单测——
  矩阵测试成本极低，没有借口省略。
- 矩阵落地为 `tests/unit/common/test_filter_contract_matrix.py`（mirror
  `common/type_def/filter.py`），加算子时给矩阵加行、CI 红灯即拦截。

## 5. 军规三：连坐测试（动共享层的回归义务）

改 L1/L2（枚举、编译器、求值器、哨兵约定）的 PR，合并前必须同时跑绿：

- [ ] `pytest -m unit`（全量单测）
- [ ] 检索侧回归：`pytest tests/unit/retrieval/`
- [ ] 演进侧回归（dreaming 落地后补 `tests/unit/control/`）
- [ ] 表态矩阵（§4）更新且通过

对应 AGENTS.md 约束 #2：跨模块规约变动必须同步修订 specs——filters 契约的
spec 文档里"最近一次修订日期"填当天。

## 6. 冻结令：`_BUILTIN_FIELDS` 只减不增（对应 F04 审计 #1）

**禁止往 13 内置字段白名单加新成员**。裸名吞噬链路：

```
新标量字段需求 → 有人往 _BUILTIN_FIELDS 加 "priority"
→ 存量用户 filters {"priority": {"eq": 5}}（今天=查 user_metadata.priority）
→ 一夜之间变成查"新内置字段 priority"
→ 静默语义翻转，无报错，存量查询悄悄变空/变错
```

正确做法：新标量字段一律进 `system_metadata.<key>` 命名空间——照样可筛
（`system_metadata.priority eq 5`）、可索引、可下推，且**不改变任何存量表达式的语义**。

## 7. 哨兵/投影约定的"三处同改"清单（对应审计 #4、#6）

以下隐式契约改一处必须同改其余处，PR 描述里逐项打勾：

| 约定 | 写侧 | 读侧（谓词） | 读侧（求值） |
|---|---|---|---|
| `T_EVENT_UNKNOWN=0` | `_index_ops.py` 投影 | `predicate_builder.py` EQ 0 放行分支 | `memory_filter._field_value` |
| `T_INVALID_OPEN` | `_index_ops.py` 投影 | `predicate_builder.py` GT 谓词 | `memory_filter._field_value` |
| ES array_marker | 写侧投影 | `elasticsearch_fulltext._scalar_match` 守卫 | （ES 内闭环） |

已知缺口：`t_message` **无哨兵约定**（真源 None 在窗口谓词下静默排外）——已定论绕开：
消费方时间窗一律用 `t_ingest`（内核接入强制盖章、恒非空）+ 后置筛选（F04 D5），
`t_message` 不作 filters 时间窗轴。

## 8. 存量瑕疵优先级（F04 §4 审计的行动版）

| 优先级 | 项 | 动作 | 时限 |
|---|---|---|---|
| **P0** | #1 裸名吞噬 | 冻结令已写入本文 §6——**立纪律，零代码** | 即刻生效 |
| **P0** | #4 t_message 无哨兵 | **已定论（2026-09-15）**：t_message 确认可空（`payload.occurred_at` 调用方可选）；dreaming 时间窗轴改 `t_ingest`（内核接入强制盖章、恒非空）+ resolver 后置筛选，**绕开哨兵需求**。详见 F04 D5。剩余动作：dreaming 实现按新轴落地；t_message 若将来进 filters 时间窗场景再议哨兵 | dreaming 实现时 |
| P1 | #6 ES array_marker 隐式契约 | 归档进 specs（现仅活在代码注释） | 下次动 specs |
| P1 | #2 bool/number 求值分歧 | specs 已知分歧清单记录"内存 1==True，ES/pg 判否"；修不修另议 | 下次动 specs |
| P2 | #5 >2^53 精度丢失 | 下次动 `_normalize_value` 时顺手拒绝超大整数 | 无限拖可接受 |
| — | #3 metadata.* 移除先例 | 无行动项——已有 `_check_field` 守卫，作为军规一的历史论据保留 | 不修 |

## 9. 快速参考卡（贴 PR 模板）

```
【filters 变更自查】
□ 我只是造句（用现有算子/字段）→ 无需继续
□ 我动了共享层 →
  □ 纯增量检查单（§2）
  □ 表态矩阵已加行（§4）
  □ 双消费方回归已跑（§5）
  □ 没碰 _BUILTIN_FIELDS（§6）
  □ 没改哨兵/投影约定；若改了，三处同改打勾（§7）
  □ specs 修订日期已更新
```
