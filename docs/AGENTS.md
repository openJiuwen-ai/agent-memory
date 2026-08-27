# agent-memory/docs

`agent-memory` 系统的设计归档目录。
本目录承载**三类长期文档**——总体设计（design）、跨模块规约（specs）与特性文档（features）。

模块内部约束放在对应的 `jiuwen_memory/<subdir>/AGENTS.md`，不在本目录下。

## 文档职责明确分工

| 文档类型 | 存放位置 | 记录什么 | 不记录什么 |
|---------|---------|---------|-----------|
| **spec（规约）** | `docs/specs/S*.md` | 接口契约、数据结构、不变量、注册机制——描述"接口应该是什么样" | ❌ 不列举具体实现（如"当前实现：xxx.py"） |
| **AGENTS.md（实现约束）** | `jiuwen_memory/<module>/AGENTS.md` | 模块地图、当前实现列表、行为铁律、本地约束——描述"当前有哪些实现" | ❌ 不写跨模块契约（归 specs） |
| **features（特性文档）** | `docs/features/F*.md` | 背景、决策、拒绝的方案、验证、已知遗留——描述"为什么这样改" | ❌ 不写接口签名（归 specs），不写实现细节（归 AGENTS.md） |

**核心原则**：
- spec 是契约（长期稳定），只写接口定义
- AGENTS.md 是实现地图，记录当前有哪些实现、文件职责、行为铁律
- features 是决策日志，记录为什么这样改、拒绝了什么方案

## 目录用途

| 目录 | 用途 | 关联触发 |
|---|---|---|
| `design/` | 总体架构与愿景：系统整体设计、技术选型、竞品分析。**长期有效**，描述"系统为什么是这样"。 | 架构方向或技术选型变化时同步更新 |
| `specs/` | 跨模块规约：子模块间的契约、协议、边界、不变量。**长期有效**，描述"系统接口是什么样"。 | 任何**跨模块接口或不变量变动**都必须同步更新；新规约 = 新 spec 文件 |
| `features/` | 特性文档：每次影响公开接口、跨模块协调或有多方案取舍的特性落地时，记录来龙去脉与架构决策。**只增不删**，描述"为什么这样改"。 | 影响公开接口、跨模块协调或有多方案取舍的特性提交代码前必须归档 |

`specs/` 是状态快照（描述当前），`features/` 是特性日志（描述过程）。spec 改了必须对应一份 features 文档解释"为什么改成这样"。

## 命名规约

```
specs/    SNN-<slug>.md     （如 S01-memory-scope.md）
features/ FNN-<slug>.md     （如 F01-memory-lifecycle-manage.md）
```

- `NN` 是两位递增序号（`01`、`02`、…），不复用、不回填；specs 前缀 `S`，features 前缀 `F`
- `<slug>` 短横线分隔的英文小写，描述主题
- 文件名不带日期——日期写在文档元信息里，不写 commit hash

不允许：序号跳号、大写字母、空格、无序号的散落 markdown。

## 内容约定

### specs/ 文档骨架

```markdown
# <Spec 名称>

## 元信息
| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/<path> |
| 最近一次修订日期 | <YYYY-MM-DD> |
| 关联特性文档 | FNN-<slug>.md（可选） |

## 范围 / 边界
（这个规约管什么、不管什么）

## 不变量
（系统在任意时刻必须为真的事实）

## 接口契约
（公共 API 形态、参数语义、错误语义）
❌ 不写"当前实现：xxx.py"——实现列表归 jiuwen_memory/<module>/AGENTS.md

## 数据结构
（关键状态字段及其生命周期）

## 与其它 spec 的关系
```

### features/ 文档骨架

```markdown
# <特性名称>

## 元信息
| 项 | 值 |
|---|---|
| 日期 | YYYY-MM-DD |
| 影响范围 | jiuwen_memory/<path>，docs/specs/SNN-<slug>.md（如有） |
| 测试基线 | <pytest 结果> |
| Refs | #<issue>（如有） |

## 背景
（这个改动为什么必要——上下文、历史、痛点）

## 决策
（选了什么方案，关键的权衡）

## 拒绝的方案
（评估过但没选的 + 为什么没选——避免后人重蹈覆辙）

## 验证
（测试 / lint / 行为基线）

## 已知遗留
（这次没做但应该做的 follow-up）
```

**核心是"决策"和"拒绝的方案"**——commit message 写了 what，features 文档要写 why-not。缺这两节等于没归档。


## 提交约定

影响公开接口、跨模块协调或有多方案取舍的特性，固定**三个连续提交**：

1. `feat(memory): <实现>` — 功能代码
2. `test(memory): <测试>` — 测试代码
3. `docs(memory): <归档>` — `features/FNN-*.md` 新增 + 受影响 `specs/SNN-*.md` 修订日期更新 + 受影响 `jiuwen_memory/<subdir>/AGENTS.md` 更新

纯内部小改动允许提交 1+2 合并，但提交 3（文档）仍然必须。

footer 用 `Refs: #<issue>` 关联 issue；issue 号无法确认时先询问，不要臆造。

## 反模式

- **把 commit message 复制粘贴当特性归档**：features 文档要写 commit 写不下的东西（拒绝的方案、遗留），不是 commit 的拷贝
- **靠 docs 维护代码行为**：实现行为是代码 + 单测的事；docs 描述意图与决策，不是 source of truth
- **不更新 specs**：模块契约改了但 spec 没改，下次读 spec 的人会被误导。提交前自查
- **一次变动拆成多份 features 文档**：一次连贯的设计变动归档为一份，多 commit 落地的特性也只归一份
- **在元信息里写 commit hash**：只写日期（`YYYY-MM-DD`），hash 容易过时
