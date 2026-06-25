# agent-memory

结构化记忆系统 —— 负责记忆的摄入、构建、检索与生命周期管理。

## 项目结构

```
.
├── src/                # 内核源码（接口/接入/构建/检索/编排/存储/共享插件）
├── tests/              # 测试（unit / integration，路径镜像 src/）
├── bootstrap/          # 接入形态实现（CLI 客户端 / SDK / MCP Server）
├── docs/               # 设计文档归档（design / specs / features）
├── agent_plugin/       # 外部 agent 插件适配（codex / hermes / openclaw / JiwenSwarm）
├── deploy/             # 部署配置（docker / local）
├── evaluation/         # 评测框架（benchmark / metrics / smoke_test）
├── examples/           # 使用示例与快速上手
├── scripts/            # 运行脚本（run-cli / run-server）
└── .claude/            # AI 辅助工具配置（rules / skills / settings）
```



## AGENTS.md 层级

```
AGENTS.md              ← 本文件：项目入口，目录结构 + 测试/代码风格/归档/提交约定
src/AGENTS.md          ← 内核总览：模块地图 + 数据流 + 架构铁律 + 子模块 AGENTS.md 创建规则
src/<subdir>/AGENTS.md ← 模块本地：代码目录结构、文件关系、入口、行为铁律、本地约束
docs/AGENTS.md         ← 文档归档规约：文档目录结构、命名规则、文档骨架、提交约定
```

职责边界：
- **本文件**：新人入口，描述项目是什么、怎么构建、代码风格、提交规范
- **`src/AGENTS.md`**：内核开发者入口，描述模块间关系和数据流走向
- **`src/<subdir>/AGENTS.md`**：模块本地规约，只写相对稳定的结构性约束（模块职责边界、行为铁律、本地约束、文件关系），不写会频繁变化的特性设计
- **`docs/specs/`**：跨模块接口规约（接口契约、协议、不变量、公共 API），相对稳定
- **`docs/features/`**：特性设计文档（决策、方案取舍、验证基线、已知遗留），承载会变化的特性层面内容
- **`docs/AGENTS.md`**：只讲归档结构与命名规约，不重复模块设计内容

## 测试

- 单测路径镜像源码：`src/retrieval/retriever.py` → `tests/unit/retrieval/test_retriever.py`
- 集成测试放 `tests/integration/<模块>/`，验证跨层链路。
- 存储后端用内存实现（各 Store 的 in-memory 变体）做单测，避免依赖外部服务。
- `pytest` 纯函数风格；不使用 `print`，断言失败信息写在 `assert ... , "msg"` 里。
- 公共 fixtures 放 `tests/conftest.py`；模块级辅助放各子目录的 `fixtures.py`。
- **Markers**：`@pytest.mark.unit`（快速确定性）、`@pytest.mark.integration`（跨组件，可能跳过）。
- **运行**：`pytest`（全量）；`pytest -m unit`（仅单测）；`pytest tests/unit/retrieval/`（按模块）。

## 代码风格

- **类型注解**：使用 PEP 585 内置泛型（`list[X]`、`dict[K, V]`）和 PEP 604 联合类型（`X | Y`、`X | None`）；禁止从 `typing` 导入 `List` / `Dict` / `Set` / `Optional` / `Union` 等已被内置语法替代的别名。`Callable`、`Awaitable`、`TYPE_CHECKING`、`Any` 等无内置等价物的仍从 `typing` 导入。
- **Linter**：`ruff`（行长 100，target Python 3.11）；提交前运行 `ruff check --fix` 修复，`ruff check` 验证。

## 设计文档归档与双向同步

把 **`src/` 下的代码和 `AGENTS.md` + `docs/specs/` + `docs/features/`** 当作一个一致性单元维护。

**文档范畴**：
- **`src/<subdir>/AGENTS.md`**：模块本地规约，只写相对稳定的结构性约束（模块地图、行为铁律、本地约束、与其他模块的边界）。这些是模块的"不变量"——除非重构否则不会变。
- **`docs/specs/SNN-*.md`**：跨模块接口规约（接口契约、协议、公共 API、架构铁律）。这些是"系统层契约"——变化需要跨模块协调。
- **`docs/features/FNN-*.md`**：特性设计文档（决策上下文、方案取舍、验证基线、已知遗留）。这些是"会演进的设计"——承载特性层面的变化和历史。

三条强制约束，提交时必查（由 `.githooks/pre-commit` 自动检查）：

1. **影响公开接口、跨模块协调或有多方案取舍的特性必须归档特性文档，特性代码、测试代码、文档拆成三个连续提交**：在 `docs/features/` 下新增 `FNN-<slug>.md`，记录决策、拒绝的方案、验证基线、已知遗留。落地顺序**固定为三个紧邻的提交**——提交 1 落特性代码（`feat(memory): ...`），提交 2 落单测（`test(memory): ...`），提交 3 落文档（`docs(memory): ...`，含 features 新增、受影响 specs 修订日期更新、受影响模块 AGENTS.md 更新）。特性代码、测试、文档不再混进同一次 commit——既不让大段文档 diff 淹没代码评审，也让测试改动独立可审，还避免文档归档拖延导致设计上下文随时间漂移。commit message 只写 what，features 文档负责写 why / why-not。

2. **跨模块规约变动必须更新 specs 文档**：接口契约、跨模块协议、不变量、公共 API 发生变化时，同步修订 `docs/specs/SNN-<slug>.md`；新规约 = 新 spec 文件。规约变了但 specs 没改，下次读 spec 的人就被误导——这是设计债，不是文档懒。

3. **双向同步：读到与代码不一致的描述必须当场修文档**。以代码为准刷新文档，在同一次改动里落地；不要把过时表述当作新约束执行。任何一份 `AGENTS.md` / `docs/specs/*` / `docs/features/*` 里读到的接口名、枚举值、truth table 行数、文件路径、不变量等只要与当前代码不符，**不要**把过时表述当作新约束去执行、也不要原样转述给用户；先 `grep` 代码、以代码为准刷新文档，在同一次改动里落地。`AGENTS.md` 里每条点名了"X 个分支 / Y 路 dispatch / Z 方法"的句子都是契约的一部分。**更新目标是 `AGENTS.md`，不是 `CLAUDE.md`**——`CLAUDE.md` 现在只是 `@AGENTS.md` 的单行壳，编辑它没有任何意义；所有内容变更一律落到对应目录的 `AGENTS.md`。

**pre-commit hook 自动检查**：
- 源码变更（.py/.yaml/.yml）但未包含文档更新时提示
- 提示检查对应模块的 AGENTS.md / docs/specs / docs/features
- spec 文件变更时检查是否更新了修订日期
- 可用 `--no-verify` 跳过检查（确认无需更新文档时）

收尾规范：
- **设计文档优先使用中文撰写**（代码标识符、命令、文件路径保持英文原名）。
- spec 头部"最近一次修订日期"字段在每次修订该 spec 时填当天日期（`YYYY-MM-DD`）；features 文档用
  头部"日期"字段记录归档当天，不设独立修订字段。**不要在元信息里写 commit hash**——避免"提交后
  回填"的来回反复。
- 子模块自身的本地约定继续放各 `src/<subdir>/AGENTS.md`；跨子模块的设计规约一律落到 `docs/specs/`，
  不要塞进单一子目录的 AGENTS.md。
- **`CLAUDE.md` 是只读壳，不要编辑它**：本模块每个子目录的 `CLAUDE.md` 仅含 `@AGENTS.md` 一行，
  编辑 `CLAUDE.md` 的修改不会被保留在任何有效文档里。需要更新文档时，直接编辑 `AGENTS.md`。
- 拿不准某次改动是否需要归档（影响公开接口？跨模块协调？多方案取舍？）时，先问用户。
  歧义情况默认**归档**——多一份 markdown 的成本远低于丢失设计上下文。

## 提交约定

commit message scope 固定用 `memory`（如 `feat(memory): ...`、`fix(memory): ...`）。

footer 用 `Refs: #<issue>` 关联 issue；issue 号无法从上下文确认时必须先询问用户，不要臆造。

涉及文档更新的特性改动，特性代码、测试、文档拆成三个连续提交，细则见上文「设计文档归档与双向同步」约束 #1。
