# 动态抽取与落盘前巩固

## 元信息
| 项 | 值 |
|---|---|
| 日期 | 2026-07-25 |
| 影响范围 | src/construction/，src/control/，docs/specs/S02-memory-api.md，docs/specs/S03-memory-manage.md，docs/specs/S05-construction.md |
| 测试基线 | 见“验证” |

## 背景

旧 `llm_extractor` 把 fact/event/preference/context 固化在单个 system prompt 中，调用方
无法按业务场景增加新的抽取策略。与此同时，候选落盘前的相似召回、ADD/UPDATE/
SUPERSEDE/NOOP 判定和写入动作长期藏在 Evolver 内，职责上属于隐式 consolidation，
却没有独立接口，也无法按本次 write 传入策略。

## 决策

1. 保留旧 Extractor 实现，新增 `dynamic_llm`。动态实现从 write metadata 读取任意
   `_extract_prompt_<strategy>`；`infer=true` 仍是抽取开关，每个策略执行一次 LLM。
   metadata prompt 作为 system prompt 原样发送，由调用方定义响应格式；候选记录
   `_extraction_strategy`。
2. `DynamicLLMExtractor` 采用模板方法：基类统一处理策略遍历、LLM 调用、fallback、
   策略标记和 consolidation prompt 透传；默认 `parse_response` 按 JSON 解析，子类覆盖
   该方法解析 XML 或其它格式并转换为 `list[MemoryUnit]`。格式相关中间结构只存在于
   子类内部，不改变 Extractor 对 Evolver 的统一输出契约。
3. 新增 `Consolidator` 接口。`consolidation_1` 承接旧 Evolver 的阈值短路、LLM 四态
   判定、content merge 和 KV/Index 副作用；Dedup 继续只负责召回。
4. `consolidation_2` 读取 `_consolidation_prompt_<strategy>`，优先与动态抽取策略同名
   配对，并追加固定四态输出 schema。无 prompt、解析失败或 existing id 非法时回退
   `consolidation_1`，采用“宁可 ADD，不丢信息”的兼容原则。
5. 默认单 pipeline 的所有 write 在最终落盘前统一经过 `consolidation_2`。显式
   pipeline profile 必须绑定自己的 Consolidator；旧 profile 未绑定时保持原直写路径，
   防止错误复用默认 profile 的 KV/Index/Dedup。
6. metadata 是 `dict[str, str]`。调用方使用赋值或 `update` 传 prompt；不新增
   `metadata.append()` 这种与 Python 字典不兼容的 API。

## 拒绝的方案

- **直接修改旧 `llm_extractor` prompt**：仍会把策略固化在代码中，也会破坏已有配置，
  因此保留旧实现并新增路由实现。
- **把 Dedup 一并合入 Consolidator**：召回后端选择与业务决策是两个变化轴，合并后
  难以在 vector/fulltext 间复用，继续保持 Dedup 纯召回。
- **在基类中枚举 JSON/XML 等格式**：格式会持续扩展，集中分支会让内核依赖业务协议；
  因此响应格式由 metadata prompt 定义，子类只覆盖解析逻辑，基类只约束最终产出
  `list[MemoryUnit]`。
- **修改 Extractor 返回类型以暴露解析结果**：会把 XML 节点、JSON 字典等格式细节传播
  到 Evolver/Consolidator，破坏统一编排契约，因此中间结构必须在子类内转换完毕。
- **Engine 内直接调用 LLM**：会破坏控制层只编排的边界；Engine 只委托构建层算子。

## 验证

- 动态 Extractor：metadata prompt 原样发送、任意策略、多策略逐次调用、默认 JSON、
  XML 子类解析、策略标记、consolidation prompt 透传、单策略失败隔离、无 prompt 回退。
- Consolidator：consolidation_1 ADD 与四态回归；consolidation_2 同名策略、固定 schema、
  非法响应回退。
- Engine：普通 write 统一巩固，完全重复写入返回 NOOP 空结果。
- `pytest -m unit`：111 passed、462 deselected。
- `pytest`：504 passed、55 skipped；14 个失败均来自当前环境缺少 `jieba` /
  `FlagEmbedding` 可选依赖。
- 新增/重构文件 `ruff check` 通过；全仓 Ruff 仍有既存行长问题。

## 已知遗留

- CLI/MCP 尚未为 metadata prompt 提供专用参数，当前通过 Python API、HTTP payload 或
  batch NDJSON 传递。
- prompt 会随 MemoryUnit metadata 持久化；后续可引入调用级 ephemeral metadata，
  在落盘前剥离敏感 prompt。
- 旧 pipeline profile 未声明 Consolidator 时仍走兼容直写；迁移配置后才启用按 profile
  隔离的动态巩固。
