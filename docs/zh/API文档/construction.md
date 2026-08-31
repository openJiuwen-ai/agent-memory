# Construction 层 API

Construction 层接收 `MemoryUnit`，完成分类、信息提取、抽象、关联、分层标注、去重判定编排与索引构建。记忆本体及其检索索引的写入统一通过 `IndexBuilder` 进入 Storage 接口。

本文是当前抽象接口、公共类型、内置实现与配置 target 的 API 参考。以下源码是最终依据：

- [`base.py`](../../../jiuwen_memory/construction/base.py)
- [`extractor.py`](../../../jiuwen_memory/construction/extractor.py)
- [`abstractor.py`](../../../jiuwen_memory/construction/abstractor.py)
- [`associator.py`](../../../jiuwen_memory/construction/associator.py)
- [`classifier.py`](../../../jiuwen_memory/construction/classifier.py)
- [`index_builder.py`](../../../jiuwen_memory/construction/index_builder.py)
- [`dedup.py`](../../../jiuwen_memory/construction/dedup.py)
- [`layer_annotator.py`](../../../jiuwen_memory/construction/layer_annotator.py)
- [`evolver.py`](../../../jiuwen_memory/construction/evolver.py)
- [`prompt_registry.py`](../../../jiuwen_memory/construction/prompt_registry.py)
- [`common/type_def/feature.py`](../../../jiuwen_memory/common/type_def/feature.py)
- [`common/errors.py`](../../../jiuwen_memory/common/errors.py)

## 1. Construction 层调用关系

Construction 算子通常由 Control 层的 `MemoryEngine` 和 `Evolver` 编排，业务调用方优先通过 `MemoryAPI` 使用，不需要手工串接所有算子。

```text
MemoryEngine.write
  -> Ingestor 产出 MemoryUnit
  -> 可选 Classifier.classify
  -> IndexBuilder.build
       -> Storage.add / Store 端口

MemoryEngine.evolve / 后台 Job
  -> Evolver.evolve
       -> Extractor / Abstractor / Associator
       -> 可选 LayerAnnotator
       -> Dedup.recall
       -> IndexBuilder.build / update / remove
```

Construction 层不执行鉴权，也不负责面向用户的检索。鉴权属于 API/Control 边界，普通检索属于 Retrieval 层；`Dedup` 仅为演进判定直接访问索引，不经过 Retrieval Recaller。

## 2. ConstructionOperator 基类

```python
from jiuwen_memory.construction.base import ConstructionOperator, OperatorType
```

所有 Construction 算子继承 `ConstructionOperator`：

| API | 返回值 | 说明 |
|---|---|---|
| `operator_type()` | `OperatorType` | 返回算子的自描述类型 |
| `health()` | `None` | 健康时返回 `None`，失败时抛异常 |

`OperatorType` 当前包含：

- `EXTRACTOR`
- `ABSTRACTOR`
- `ASSOCIATOR`
- `CLASSIFIER`
- `INDEX_BUILDER`
- `EVOLVER`
- `LAYER_ANNOTATOR`

`Dedup` 有独立 Producer，但当前没有独立的 `OperatorType.DEDUP`；两个内置 Dedup 实现的 `operator_type()` 均返回 `EVOLVER`。

## 3. Extractor API

```python
from jiuwen_memory.construction.extractor import Extractor

derived = extractor.extract(units, context=context)
```

### `extract(units, *, context=None) -> list[MemoryUnit]`

从本轮原始 `MemoryUnit` 中提取零条或多条低抽象粒度派生记忆。派生单元应通过 `provenance` 回指来源。

`context` 类型为 `ExtractContext | None`：

```python
@dataclass
class ExtractContext:
    recent_originals: list[MemoryUnit]
    related_memories: list[MemoryUnit]
```

| 字段 | 作用 |
|---|---|
| `recent_originals` | 最近的 infer 原文，仅用于指代消解和语境增强，不参与去重，也不是提取来源 |
| `related_memories` | 已召回的相关派生记忆，用于提示已有事实和辅助去重 |

只有 `units` 是本次提取来源；两类上下文都不应被写入新单元的 `provenance`。

## 4. Abstractor API

```python
from jiuwen_memory.construction.abstractor import Abstractor

abstracted = abstractor.abstract(units)
```

### `abstract(units: list[MemoryUnit]) -> list[MemoryUnit]`

把低/中抽象记忆概括为画像、长期偏好、模式或技能等高抽象粒度记忆。产物必须保留来源 `provenance`，保证可重建和可回溯。

## 5. Associator API

```python
from jiuwen_memory.construction.associator import Associator

relations = associator.associate(units)
```

### `associate(units: list[MemoryUnit]) -> list[Relation]`

发现实体共指、主题关联、因果关系或引用关系，返回 `Relation` 列表。`Relation` 主要包含 `source_id`、`target_id`、`relation`、`score` 和 `metadata`，后续由 Evolver/索引构建链写入图索引。

## 6. Classifier API

```python
from jiuwen_memory.construction.classifier import Classifier

classified = classifier.classify(units)
```

### `classify(units: list[MemoryUnit]) -> list[MemoryUnit]`

为一批记忆设置 `tier`、主题标签和重要度等分类信息，返回更新后的单元。当前标准 Engine 仅在 `infer=false` 直写路径调用 Classifier；`infer=true` 的派生单元由 Extractor 产出分类结果。

## 7. IndexBuilder API

```python
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
```

`IndexBuilder` 是 Construction 层的统一写入入口。当前标准 `InMemoryEngine` 与 `CloudEngine` 不会先直接调用 `Storage.add/update/delete` 再调用 builder，因此 `IndexBuilder` 实现负责把本次操作完整交付到其配置的 Storage/Store。

### 7.1 写入范围

| `IndexWriteMode` | 语义 |
|---|---|
| `ALL` | 写入记忆本体和全部已启用检索索引 |
| `FORWARD_ONLY` | 只回写记忆本体，不修改检索索引；用于生命周期状态回写 |
| `RETRIEVAL_ONLY` | 只维护检索索引，不修改记忆本体；用于补建或迁移索引 |

### 7.2 删除范围

| `IndexRemoveMode` | 语义 |
|---|---|
| `HARD` | 物理删除记忆本体与检索索引 |
| `SOFT` | 只移出检索索引；本体保留，`get/list` 仍可读取 |

### 7.3 方法

| API | 返回值 | 说明 |
|---|---|---|
| `build(units, *, mode=ALL)` | `None` | 新建一批记忆及其索引 |
| `update(units, *, mode=ALL)` | `None` | 增量更新记忆及其索引 |
| `remove(units, *, mode=HARD)` | `None` | 按单元自带 Scope 幂等删除 |
| `rebuild()` | `None` | 从真源全量重建派生索引；具体实现可暂时为空操作 |

`FORWARD_ONLY`、`RETRIEVAL_ONLY` 和 `SOFT` 是接口语义，最终是否能拆分执行取决于具体 `IndexBuilder` 和 Storage 实现。

### 7.4 内置 builder 的职责边界

| `target` | 写入职责 |
|---|---|
| `forward` | 只通过 Storage 的 KV 正排端口交付记忆本体 |
| `fulltext` | 只构建全文及 L0/L1 全文索引，不交付记忆本体 |
| `vector` | 切分、向量化并构建内容及 L0/L1 向量索引，不交付记忆本体 |
| `hybrid` | 默认编排器；依次组合 forward、fulltext、vector 和可选 entity 子 builder |
| `unified` | 按 Scope 分组，将 `build/update/remove` 直接委托给 `Storage.add/update/delete` |

`forward`、`fulltext` 或 `vector` 作为独立 target 时只负责表中对应的一侧。普通完整写入应使用 `hybrid`，或使用本身能够完整实现 Storage 写入语义的 `unified + Storage` 组合。

`hybrid.rebuild()` 和 `unified.rebuild()` 当前均返回 `None`，尚未提供真实全量扫描重建流程。

## 8. Dedup API

```python
from jiuwen_memory.construction.dedup import Dedup

similar = dedup.recall(candidate)
```

### `recall(candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]`

对一条候选召回已有相似记忆，返回按分数降序排列的 `(MemoryUnit, score)`。内置实现负责：

1. 构造 Vector 或 Fulltext Store 查询；
2. 加载记忆本体；
3. 过滤候选自身和非 ACTIVE 单元；
4. 按 unit 聚合取最大分；
5. 应用 `min_similarity`。

Dedup 只召回，不决定 `ADD/UPDATE/SUPERSEDE/NOOP`。判定与落盘由 Evolver 负责。去重是 best effort，内置实现遇到异常会返回空列表而不是阻断演进。

## 9. LayerAnnotator API

```python
from jiuwen_memory.construction.layer_annotator import LayerAnnotator

annotated = annotator.annotate(units)
```

### `annotate(units: list[MemoryUnit]) -> list[MemoryUnit]`

为已有单元生成 `unit.layers.l0` 和 `unit.layers.l1`，不创建新的记忆单元。只有 `len(content) > layers_threshold` 的单元才标注；短内容保持空 layers，由 Retrieval 披露阶段回退生成。

内置实现为 best effort：单条或单批失败时保留空 layers，不阻断 write/update/evolve。

## 10. Evolver API

```python
from jiuwen_memory.construction.evolver import EvolveMode, Evolver, EvolveResult

result = evolver.evolve(units, EvolveMode.EXTRACT)
```

### `evolve(units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult`

执行指定的记忆内容演进阶段：

| `EvolveMode` | 作用 |
|---|---|
| `EXTRACT` | 从原始输入提取事实、事件、偏好或过程记忆 |
| `ASSOCIATE` | 发现关联并维护图关系 |
| `CONSOLIDATE` | 抽象、合并或冲突消解 |
| `FORGET` | 筛选低价值或被替代记忆，回写生命周期并移出检索 |

索引维护不是独立 EvolveMode；索引随 `IndexBuilder.build/update/remove` 维护。

`EvolveResult` 字段：

| 字段 | 说明 |
|---|---|
| `created_ids` | 新增记忆 ID |
| `updated_ids` | 原地更新记忆 ID |
| `superseded_ids` | 被新版本取代的旧记忆 ID |
| `forgotten_ids` | 被标记遗忘的记忆 ID |

`OrchestratingEvolver` 和 `DynamicEvolver` 是平级 target。`DynamicEvolver` 继承前者，只替换 `EXTRACT` 为 `extract -> consolidate（判定）-> reflect -> 落盘`；其他三种模式沿用父类实现。

## 11. PromptRegistry 与动态 prompt

```python
from jiuwen_memory.construction.prompt_registry import PromptRegistry

registry = PromptRegistry.from_dict(prompts)
text = registry.get("extract", "preference")
```

| API | 返回值 | 说明 |
|---|---|---|
| `PromptRegistry.from_dict(data, *, config_source=None)` | `PromptRegistry` | 从配置的 `prompts` 段构造，可注入运行时 ConfigSource |
| `get(phase, key)` | `str \| None` | 优先查询运行时 `prompts.<phase>.<key>`，再读取构造期快照 |
| `has_phase(phase)` | `bool` | 仅判断构造期快照是否包含该 phase |

支持的 phase 为 `extract`、`consolidate` 和 `reflect`。调用级 metadata 使用以下键：

| 键格式 | 作用 |
|---|---|
| `_extract_prompt_<strategy>` | 指向 `prompts.extract` 下的命名 key |
| `_consolidation_prompt_<strategy>` | 指向 `prompts.consolidate` 下的命名 key |
| `_reflect_prompt_<strategy>` | 指向 `prompts.reflect` 下的命名 key |
| `_extraction_strategy` | 派生单元记录实际使用的抽取策略 |

metadata 保存的是 prompt key，不应保存整段 prompt 文本。`DynamicLLMExtractor` 在 registry 缺少对应 key 时，会把 metadata 值本身当作文本使用以兼容旧配置。

## 12. Producer 与配置命名空间

| Producer | `TOP_NAME` | 实现目录 |
|---|---|---|
| `ExtractorProducer` | `extractor` | `extractor_impl/` |
| `AbstractorProducer` | `abstractor` | `abstractor_impl/` |
| `AssociatorProducer` | `associator` | `associator_impl/` |
| `ClassifierProducer` | `classifier` | `classifier_impl/` |
| `IndexBuilderProducer` | `constructor` | `index_builder_impl/` |
| `DedupProducer` | `dedup` | `dedup_impl/` |
| `LayerAnnotatorProducer` | `layer_annotator` | `layer_annotator_impl/` |
| `EvolverProducer` | `evolver` | `evolver_impl/` |

新实现通过 `@XxxProducer.register("target")` 注册，由 `construction.bootstrap.register_constructors()` 统一触发实现模块导入。

配置使用两级命名空间：

```yaml
constructor:             # Producer.TOP_NAME
  default:               # 具名实例
    target: hybrid       # 注册名
    params:
      storage: default   # 对其他命名空间具名实例的引用
      chunker: default
      embedder: default
```

用户配置会覆盖内置默认中的同名实例，实例 `params` 是整体替换而不是逐字段深合并。覆盖 `constructor.default`、`evolver.default` 等实例时，应把仍需要的依赖引用一并写回。HTTP/部署配置中的这些段位于 `memory_api:` 下。

## 13. 可配置实现

### 13.1 Extractor 实现

| `target` | 实现类 | 功能 | 依赖与主要参数 |
|---|---|---|---|
| `keyword` | `KeywordExtractor` | 按 Chunker 切分原文，生成带血缘的 SEMANTIC 单元；procedural 时合并为一条过程记忆 | `chunker`，默认 `fixed_window` |
| `llm` | `ExtractorImpl` | LLM 结构化抽取，校验来源、置信度、tier 和 tags | `llm`；`extractor_min_confidence`、`extractor_retry_max`、`extractor_retry_backoff`、`extract_batch_size` |
| `dynamic_llm` | `DynamicLLMExtractor` | 按 `_extract_prompt_<strategy>` 逐策略抽取；无策略时委托 fallback | `llm`、`fallback`、`prompts`；参数同 `llm` |
| `video_memory` | `VideoMemoryExtractor` | 将视频规约结果转换为 CLM/ELM 多模态 MemoryUnit | 无配置依赖；输入需包含约定的视频 metadata |

### 13.2 Abstractor、Associator 与 Classifier 实现

| 命名空间 | `target` | 实现类 | 功能 | 依赖与主要参数 |
|---|---|---|---|---|
| `abstractor` | `concat` | `ConcatAbstractor` | 把至少两条 ACTIVE 记忆拼接为一条 CORE 画像 | 无 |
| `abstractor` | `llm` | `LLMAbstractor` | 分组后由 LLM 生成 summary/pattern/portrait 等高抽象候选 | `llm`、`feature_extractor`；置信度、分组下限、批大小、上下文预算、重试参数 |
| `associator` | `keyword` | `KeywordAssociator` | 共享关键词数达到阈值时生成 `related` 关系 | `feature_extractor`；当前配置 builder 使用默认 `min_overlap=2` |
| `associator` | `llm` | `LLMAssociator` | 向量、关键词、实体三层发现并可用 LLM 深度验证 | `llm`、`feature_extractor`、`embedder`；相似度、确认区间、批大小和重试参数 |
| `classifier` | `keyword` | `KeywordClassifier` | 关键词启发式设置 tier 和主题标签 | 无 |
| `classifier` | `llm` | `LLMClassifier` | 一次 LLM 调用批量生成 tier 与 tags | `llm`；`classifier_retry_max`、`classifier_retry_backoff` |

纯离线默认 LLM 为 `echo`，不具备真实结构化推理能力。若希望 `llm` Classifier/Extractor/Abstractor/Associator 产出有效结果，应配置能够满足其 JSON 契约的真实 LLM。

### 13.3 IndexBuilder 实现

| `target` | 实现类 | 依赖与参数 | 说明 |
|---|---|---|---|
| `forward` | `ForwardIndexBuilder` | `storage` | 仅正排记忆本体 |
| `fulltext` | `FulltextIndexBuilder` | `storage`；`layers_index_enabled` | 全文与可选 L0/L1 全文索引 |
| `vector` | `VectorIndexBuilder` | `storage`、`chunker`、`embedder`；`layers_index_enabled` | 内容 chunk 向量与可选 L0/L1 向量索引 |
| `hybrid` | `HybridIndexBuilder` | `storage`、`chunker`、`embedder`；`layers_index_enabled`、`entity_enabled`、可选 `entity_store` | 默认完整编排器 |
| `unified` | `UnifiedIndexBuilder` | `storage` | 将全部 CRUD 和 mode 原样委托给 Storage |

`EntityIndexBuilder` 是 `hybrid` 内部子 builder，不注册为独立 `constructor` target。只有 `entity_enabled=true` 且成功装配 `EntityStore` 时才启用；装配失败会关闭实体链路，但全文和向量仍继续工作。

### 13.4 Dedup、LayerAnnotator 与 Evolver 实现

| 命名空间 | `target` | 实现类 | 功能 | 依赖与主要参数 |
|---|---|---|---|---|
| `dedup` | `vector` | `VectorDedup` | Embedder + Vector Store 相似召回 | `storage`、`embedder`；`dedup_min_similarity`、`dedup_top_k`、`dedup_tier_filter`、`dedup_scope_filter` |
| `dedup` | `keyword` | `KeywordDedup` | Fulltext Store 召回后用词重叠率计分 | `storage`；参数同 vector |
| `layer_annotator` | `keyword` | `KeywordLayerAnnotator` | 规则生成 L0/L1 | `layer_annotator_threshold`、`layer_annotator_l1_chars` |
| `layer_annotator` | `llm` | `LLMLayerAnnotator` | LLM 批量生成并严格校验 L0/L1 | `llm`；阈值与重试参数 |
| `evolver` | `orchestrating` | `OrchestratingEvolver` | legacy 四模式；EXTRACT 中去重判定与落盘耦合 | extractor、abstractor、associator、index_builder、storage、message_store、dedup、llm；可用 `params.layer_annotator` 选择、禁用标注器 |
| `evolver` | `dynamic` | `DynamicEvolver` | 动态 prompt 四步 EXTRACT；其他模式继承 orchestrating | 同上，额外使用 `PromptRegistry`；存在 `layer_annotator.default` 时自动注入 |

两个 Evolver 都使用 `dedup_medium_similarity`（默认 `0.7`）与 `dedup_high_similarity`（默认 `0.9`）。`vector_enabled=false` 时，未显式指定的 IndexBuilder/Dedup 默认分别切换为 `fulltext` 和 `keyword`。

## 14. 默认装配

无用户配置时，Construction 相关默认实例为：

| 命名空间 | 默认 target | 备注 |
|---|---|---|
| `extractor.default` | `dynamic_llm` | 无调用级策略时 fallback 到 `extractor.legacy=keyword` |
| `abstractor.default` | `concat` | 规则画像合并 |
| `associator.default` | `keyword` | 关键词关联 |
| `classifier.default` | `llm` | 使用共享 `llm.default`；离线默认为 echo |
| `constructor.default` | `hybrid` | storage + chunker + embedder |
| `dedup.default` | `vector` | storage + embedder |
| `evolver.default` | `orchestrating` | 默认 legacy EXTRACT |
| `evolver.dynamic` | `dynamic` | 已声明具名实例，但不会自动替代 default |
| `layer_annotator` | 未默认声明 | Evolver 未找到具名 default 时不标注 |

## 15. 动态演进配置示例

```yaml
prompts:
  extract:
    preference: "抽取用户偏好，输出约定 JSON"
  consolidate:
    preference: "判断候选是新增、更新、取代还是忽略"
  reflect:
    preference: "落盘前检查并修正候选"

extractor:
  default:
    target: dynamic_llm
    params:
      llm: default
      fallback: legacy
  legacy:
    target: keyword
    params:
      chunker: default

layer_annotator:
  default:
    target: llm
    params:
      llm: default
      layer_annotator_threshold: 512

evolver:
  default:
    target: dynamic
    params:
      extractor: default
      abstractor: default
      associator: default
      index_builder: default
      storage: default
      message_store: default
      dedup: default
      llm: default
      dedup_medium_similarity: 0.7
      dedup_high_similarity: 0.9
```

`dynamic` 的当前 builder 会直接查找 `layer_annotator.default`，不读取
`evolver.default.params.layer_annotator`；而 `orchestrating` 支持用该参数选择具名实例，或传空值显式禁用标注。

调用时把 prompt key 放入系统 metadata：

```python
system_metadata = {
    "infer": "true",
    "_extract_prompt_preference": "preference",
    "_consolidation_prompt_preference": "preference",
    "_reflect_prompt_preference": "preference",
}
```

## 16. 自定义实现要求

新增 Construction 算子时至少需要：

1. 继承对应抽象接口并实现业务方法；
2. 实现 `operator_type()` 与 `health()`；
3. 使用对应 Producer 的 `register("target")` 注册；
4. 通过注入实例使用 Storage、LLM、Chunker、Embedder 等依赖，不在实现中自行构造后端；
5. 派生记忆正确填写 `scope` 和 `provenance`；
6. IndexBuilder 同时实现 `build/update/remove/rebuild`，并尊重写入/删除 mode；
7. Dedup 与 LayerAnnotator 保持 best-effort 语义，不因可降级失败阻断主写入链路。

## 17. 方法级契约

本节补充“方法能被调用”之外的输入、输出和副作用约定。抽象接口不声明跨算子事务；
需要落盘原子性时，由具体 IndexBuilder/Storage 实现及其后端保证。

### 17.1 Extractor、Abstractor、Associator 与 Classifier

| API | 空输入 | 输入/输出关系 | 持久化副作用 |
|---|---|---|---|
| `extract(units, *, context=None)` | 返回空列表 | 返回新的派生单元；`context` 只增强语境，不是 provenance 来源 | 无；落盘由 Evolver/IndexBuilder 负责 |
| `abstract(units)` | 返回空列表 | 返回高抽象派生单元；内置 `concat` 仅使用 ACTIVE 输入，少于两条时不产出 | 无 |
| `associate(units)` | 返回空列表 | 只发现 `Relation`，不保证 score 统一在 `[0, 1]`；score 量纲由实现定义 | 无；图索引写入由 Evolver/IndexBuilder 编排 |
| `classify(units)` | 返回空列表 | 内置实现原地更新输入单元并返回同一列表的单元，保持 id、Scope 与顺序 | 无；Engine 后续调用 IndexBuilder 落盘 |

`Relation` 是共享数据结构：

| 字段 | 类型 | 默认值 | 语义 |
|---|---|---|---|
| `source_id` | `str` | `""` | 关联起点的记忆或实体 ID |
| `target_id` | `str` | `""` | 关联终点的记忆或实体 ID |
| `relation` | `str` | `""` | 关系名，如 `related` / `caused_by` / `refers_to` |
| `score` | `float` | `0.0` | 实现自定义的相关性或置信分 |
| `metadata` | `dict[str, Any]` | `{}` | 关系证据和其他附加属性 |

`FeatureSet` 是 Associator/Extractor 使用的共享特征容器，字段为
`keywords: list[str]=[]`、`entities: list[Entity]=[]` 和 `labels: dict[str, str]={}`。
`Entity` 包含 `text: str=""`、`type: str=""` 与 `score: float=0.0`。

自定义 Extractor/Abstractor 产出的每条单元必须有非空 ID，并保持正确的 Scope 与
`provenance`。多来源输出的 `user_metadata` 只能继承所有来源中值相等的交集。

### 17.2 IndexBuilder

```python
build(units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None
update(units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None
remove(units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD) -> None
rebuild() -> None
```

| 约定 | 说明 |
|---|---|
| 空批次 | 应当是无副作用的空操作 |
| Scope | Scope 来自每个 `MemoryUnit`；内置 builder 允许一批中包含多个 Scope，并按单元 Scope 写入 |
| 顺序 | `HybridIndexBuilder.build` 先写正排再写派生索引；`remove(HARD)` 先删派生索引再删正排 |
| 原子性 | 内置 hybrid 顺序调用多个子 builder，不提供跨 Store 原子性；中途失败可能已留下真源或部分索引 |
| 重试 | `build` 保持新建语义，重复 ID 可抛 `ConflictError`；`update` 目标不存在可抛 `NotFoundError`；`remove` 为幂等删除 |
| mode 支持 | 实现必须理解三种写 mode 和两种删除 mode；不支持的能力不得静默改成反向语义 |
| `rebuild()` | 当前内置 builder 都没有全量扫描实现，返回 `None` 不代表索引已重建 |

`UnifiedIndexBuilder` 会按五段 Scope 保持输入顺序分组，每组分别调用
`Storage.add/update/delete`。`HybridIndexBuilder` 的顺序设计优先保留可重建的记忆本体，
但并不将多后端写入包装成事务。

### 17.3 Dedup、LayerAnnotator 与 Evolver

| API | 读写性 | 返回契约 | 失败语义 |
|---|---|---|---|
| `Dedup.recall(candidate)` | 只读召回 | 按分数降序的 `(MemoryUnit, float)`；过滤 candidate 自身和非 ACTIVE 单元 | 内置实现吞掉后端异常并返回空列表 |
| `LayerAnnotator.annotate(units)` | 原地更新 `unit.layers` | 返回已处理单元；短文本可保持空 layers | 内置实现按单条/单批 best effort，标注失败不阻断主写入 |
| `Evolver.evolve(units, mode)` | 可读原文、召回去重并通过 IndexBuilder 落盘 | 返回本次已完成的 ID 分类 | 非 best-effort 的抽取/写入失败向上抛出；已完成的多 Store 副作用不自动回滚 |

`EvolveResult` 的字段类型和默认值：

| 字段 | 类型 | 默认值 | 语义 |
|---|---|---|---|
| `created_ids` | `list[str]` | `[]` | 本次新增成功的记忆 ID |
| `updated_ids` | `list[str]` | `[]` | 本次原地更新的记忆 ID |
| `superseded_ids` | `list[str]` | `[]` | 已被新版本取代的旧记忆 ID |
| `forgotten_ids` | `list[str]` | `[]` | 已标记遗忘并退出检索的 ID |

`EvolveResult` 是完成结果，不是事务回滚日志。调用抛异常时，不能仅根据未取得返回值就假定
后端没有发生任何写入。

## 18. 内置参数参考

下表仅列出当前 builder 直接读取、对运行行为有影响的通用参数；依赖引用如 `llm`、
`storage`、`embedder` 仍按前文 Producer 规则解析。

| 参数 | 类型 | 默认值 | 适用实现 | 作用/约束 |
|---|---|---:|---|---|
| `extractor_min_confidence` | `float` | `0.5` | `llm` / `dynamic_llm` | 过滤低置信抽取候选 |
| `extractor_retry_max` | `int` | `3` | `llm` / `dynamic_llm` | LLM 最大尝试次数，应大于等于 `1` |
| `extractor_retry_backoff` | `int` | `1000` | `llm` / `dynamic_llm` | 重试退避，毫秒 |
| `extract_batch_size` | `int` | `10` | `llm` / `dynamic_llm` | 单次 LLM 抽取的原文条数上限 |
| `abstractor_min_confidence` | `float` | `0.5` | `abstractor.llm` | 最低置信度 |
| `abstractor_min_group_size_summary` | `int` | `1` | `abstractor.llm` | summary 分组下限 |
| `abstractor_min_group_size_pattern` | `int` | `3` | `abstractor.llm` | pattern 分组下限 |
| `abstractor_min_group_size_portrait` | `int` | `5` | `abstractor.llm` | portrait 分组下限 |
| `abstractor_max_groups_per_batch` | `int` | `4` | `abstractor.llm` | 单次 LLM 最大分组数 |
| `abstractor_max_context_tokens` | `int` | `180000` | `abstractor.llm` | 上下文 token 预算 |
| `abstractor_retry_max` | `int` | `3` | `abstractor.llm` | LLM 最大尝试次数 |
| `abstractor_retry_backoff` | `int` | `1000` | `abstractor.llm` | 重试退避，毫秒 |
| `associator_similarity_threshold` | `float` | `0.7` | `associator.llm` | 向量候选相似度阈值 |
| `associator_keyword_jaccard_threshold` | `float` | `0.3` | `associator.llm` | 关键词 Jaccard 阈值 |
| `associator_entity_match_threshold` | `float` | `0.8` | `associator.llm` | 实体匹配阈值 |
| `associator_min_auto_confirm` | `float` | `0.5` | `associator.llm` | 自动确认区间下界 |
| `associator_max_auto_confirm` | `float` | `0.85` | `associator.llm` | 自动确认区间上界 |
| `associator_min_final_score` | `float` | `0.5` | `associator.llm` | 最终关系最低分 |
| `associator_deep_discovery` | `bool` | `true` | `associator.llm` | 是否开启 LLM 深度发现 |
| `associator_max_pairs_per_llm_call` | `int` | `10` | `associator.llm` | 单次 LLM 候选对上限 |
| `associator_ann_threshold` | `int` | `50` | `associator.llm` | 切换 ANN 候选发现的数量阈值 |
| `associator_max_units_per_associate` | `int` | `200` | `associator.llm` | 单次关联单元上限 |
| `associator_retry_max` | `int` | `3` | `associator.llm` | LLM 最大尝试次数 |
| `associator_retry_backoff` | `int` | `1000` | `associator.llm` | 重试退避，毫秒 |
| `classifier_retry_max` | `int` | `3` | `classifier.llm` | LLM 最大尝试次数 |
| `classifier_retry_backoff` | `int` | `1000` | `classifier.llm` | 重试退避，毫秒 |
| `dedup_min_similarity` | `float` | `0.5` | 两种 Dedup | 最低相似度 |
| `dedup_top_k` | `int` | `5` | 两种 Dedup | 候选上限 |
| `dedup_tier_filter` | `bool` | `false` | 两种 Dedup | 是否限制相同 tier |
| `dedup_scope_filter` | `bool` | `true` | 两种 Dedup | 是否按候选 Scope 限定召回 |
| `dedup_medium_similarity` | `float` | `0.7` | 两种 Evolver | 中相似判定阈值 |
| `dedup_high_similarity` | `float` | `0.9` | 两种 Evolver | 高相似判定阈值，应不小于 medium |
| `layer_annotator_threshold` | `int` | `512` | 两种 LayerAnnotator | 只标注 content 长度超过阈值的单元 |
| `layer_annotator_l1_chars` | `int` | `200` | `layer_annotator.keyword` | L1 截取字符数 |
| `layer_annotator_retry_max` | `int` | `3` | `layer_annotator.llm` | LLM 标注最大尝试次数 |
| `layer_annotator_retry_backoff` | `int` | `1000` | `layer_annotator.llm` | 重试退避，毫秒 |
| `layers_index_enabled` | `bool` | `true` | fulltext/vector/hybrid | 是否写入 L0/L1 独立索引 |
| `entity_enabled` | `bool` | `false` | `hybrid` | 是否尝试装配 EntityStore |
| `vector_enabled` | `bool` | `true` | Evolver builder | 决定未显式指定时的 IndexBuilder/Dedup 默认组合 |

### 18.1 `extract_batch_size` 与 `middle_batch_size`

| 参数 | 配置位置 | 默认 | 作用 |
|---|---|---:|---|
| `middle_batch_size` | `job_factory.default.params` | `10` | Job 切批上限 |
| `extract_batch_size` | `extractor` 装配 params（同 `extractor_min_confidence`） | `10` | Extractor 单次 LLM 条数上限 |

`middle_batch_size` 在 `defaults.py` 快照中；`extract_batch_size` 默认由 Extractor `_build` 提供，需覆盖时在装配 YAML 的 `extractor.*.params` 中声明。调参时保持 `extract_batch_size` ≥ `middle_batch_size`。

## 19. 最小算子示例

```python
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.classifier import Classifier
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.types import IndexWriteMode


def classify_and_build(
    classifier: Classifier,
    index_builder: IndexBuilder,
    units: list[MemoryUnit],
) -> list[str]:
    classified = classifier.classify(units)
    index_builder.build(classified, mode=IndexWriteMode.ALL)
    return [unit.id for unit in classified]
```

这是 `infer=false` 直写路径中 Construction 算子的最小组合；实际应用应让 `MemoryEngine`
进行跨层编排，不应在业务代码中复制完整写入流程。
