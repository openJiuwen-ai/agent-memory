# JiuwenSwarm 记忆适配

`AgentMemoryMemoryProvider` 提供 SDK/HTTP 两种连接方式。工作模式使用默认 `scene="agent"`；Coding 使用 `scene="code", project="宿主生成的项目键"`，搭配 core 的 `JiuwenMemoryCodingRail`。

- 实施决策：[Coding 文档记忆](../../docs/features/F08-coding-document-memory.md)。
- 调用时机、开关、项目键及旧 AutoMemory 的独立运行由 Swarm 宿主负责。

SDK 必须注入可信 `security_provider`，文档目录由 `markdown_store.default.params.root` 指定。HTTP 必须使用已启用文档记忆、可正常认证的服务。两种方式都复用原 MemoryAPI，不新增 search/evolve 平行接口。

Work/Code 的记忆 Agent 身份都优先采用运行名称，再回落配置 agent_id。SDK 中，相同 org/user/space 和 Agent 名称可跨模式共享 USER.md；不同 Agent 名称仍按 Scope 查询隔离，项目记忆按 project 筛选。session 沿用配置（默认空字符串），不强制覆盖；共用文档根不会合并身份。


## Coding 参数与使用行为

Provider 构造参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `scene` | `agent` | 内置 `agent` / `code`；未注册值抛出 `ValueError` |
| `project` | `default` | Code 项目目录键；禁止空值、点目录、路径分隔符及 NUL；Work 忽略此构造参数 |

身份通过 `initialize(user_id, scope_id, agent_id, session_id)` 绑定。session 显式值原样保留，
不自动绑定聊天会话 ID。SDK 文档读取匹配 session；影子索引搜索按 org/space/user/agent
过滤，跨 session 召回。HTTP 的身份映射见下节。

Code 工具统一使用前缀 `agent_memory_coding_memory_`，不接受旧 Work 工具别名。
项目由宿主绑定，模型不能通过工具参数改变项目或 Scope。

| 工具后缀 | 参数 | 行为 |
|---|---|---|
| `search` | `query`；`top_k=5`（1～50） | 精确搜索当前项目，不混入空项目 |
| `write` | `content` | TEXT + `infer=true`，由现有抽取器分类；不是整文件覆盖 |
| `read` | `md_filename`；`offset`、`limit` | 读取 USER.md、MEMORY.md 或 YYYY-MM-DD.md；行号从 1 开始 |
| `edit` | `md_filename`、`old_text`、`new_text` | 唯一文本替换；零次或多次匹配报错 |

USER.md 固定使用空 project，其他文件使用当前项目。write 失败返回 error，
抽取为空或去重返回 `stored=false`，有新单元才返回 `stored=true`。

`prefetch` 并行读取 USER.md 与项目 MEMORY.md；普通请求同时搜索项目，默认 `top_k=5`。
项目搜索为空、失败或超过 5 秒时，使用已读取的 MEMORY.md；背景请求只读文档。
各读取操作也有 5 秒超时，失败时对应部分为空。画像与兜底文档不受 `top_k` 限制。
`sync_turn(user, assistant)` 在两者均非空时拼接本轮内容，以 TEXT、`infer=true` 写入绑定项目；
调用时机、后台排队及关闭由 Core Rail 决定。

分类复用 `user_profile / long_term_memory / daily_memory`，文件由内核确定：

```text
{root}/USER.md
{root}/memory/{project}/MEMORY.md
{root}/memory/{project}/daily_memory/YYYY-MM-DD.md
{root}/memory/.shadow/shadow.db
```

MEMORY.md 保存正文块，不是旧 Coding 的主题索引；不自动迁移旧主题文件。
看门狗、编辑后索引更新与检索授权沿用文档记忆内核。


## HTTP 文档服务

HTTP 模式由 agent-memory 服务进程持有 Runtime、Markdown、shadow.db 和看门狗。
Swarm 只创建 HTTP 客户端；不向服务传递 sdk 配置、模型凭据或本地文档根。

服务配置示例：[document-memory-http.yaml](../../deploy/local/document-memory-http.yaml)。
在服务进程的环境中设置以下变量，再从 agent-memory 仓库启动：

| 变量 | 含义 |
|---|---|
| `MEMORY_DOCUMENT_ROOT` | 服务端独立的绝对文档目录 |
| `MEMORY_LLM_BASE_URL` / `MEMORY_LLM_API_KEY` / `MEMORY_LLM_MODEL` | 抽取模型连接参数 |
| `MEMORY_EMBED_BASE_URL` / `MEMORY_EMBED_API_KEY` / `MEMORY_EMBED_MODEL` | embedding 连接参数；地址不带 `/embeddings` |

示例的 `embedder_dim=1024` 必须与模型输出维度一致。模型、目录和认证配置由服务端管理；
更换 embedding 维度时不要直接复用已有维度的影子索引。

```bash
# 使用已安装依赖的 Python 环境；dev 认证仅用于本机联调。
scripts/run-server.sh --auth-mode dev --host 127.0.0.1 --port 8137 \
  deploy/local/document-memory-http.yaml
```

该配置开启 `write_document` 和 `watch_document`，使用 agent 抽取器、文档存储及影子检索。
启动服务会监听服务端文档目录；Swarm 不另起看门狗。服务端未开启文档记忆时，
read/edit 的失败不能解释为“文件不存在”。远端部署使用既有正式认证配置，
Swarm 的 `api_key` 和 `identity_org/identity_user` 必须与服务端认证身份匹配。

Swarm 的配置片段：

```yaml
modes:
  code:
    memory:
      enabled: true
memory:
  mode: local
  engine: builtin
  builtin:
    provider: jiuwen
    jiuwen:
      mode: http
      base_url: http://127.0.0.1:8137
      api_key: ""                 # 本地固定 dev 身份可留空；认证服务填写对应凭据
      identity_org: local
      identity_user: developer
      user_id: my-user
      agent_id: jiuwenswarm        # 无运行 Agent 名称时的兜底
```

`memory.mode=local` 是 Swarm 内置记忆门禁；`jiuwen.mode=http` 是连接方式，两者不冲突。
Work 和 Code 共用这组服务配置。四个 Coding 工具分别调用 `/v1/read_md_file`、
`/v1/add`、`/v1/edit_md_file`、`/v1/search`；回合后同步同样调用 `/v1/add`。

HTTP 沿用现有身份映射：org/user 取 identity_org/identity_user，agent 取运行名称，
session 取 user_id，space 为空；scope_id 和原 session_id 不原样传输。
同一认证身份、user_id 和 Agent 名称下画像跨 Work/Code 共享，项目事实仍按 project 隔离。
不能把更换 user_id 当作完整的多用户查询隔离：影子搜索跨 session，用户边界应由认证身份区分。
切换 SDK/HTTP 不自动迁移数据或上传本地 Markdown。

修改 SDK 文档根（`sdk.markdown_store.default.params.root`）、Swarm 本地模型/embedding
或 agent 工作区配置，不重建 HTTP Coding 记忆客户端；
服务地址、认证信息、绑定身份或项目改变时重建。服务不可达不会回退原生编码轨或本地 SDK。
写入错误表示没有确认成功；HTTP 超时不保证服务端已取消，不应盲目重复提交。
示例把单次 LLM 超时设为 90 秒、重试为 0；现有 HTTP 写等待为 120 秒，
仍需为 embedding、去重和落盘留出时间。页面回答完成不代表后台同步已经完成。


## 场景扩展与装配

Provider 的公开入参保持不变。Work/Coding 的工具、预取、项目归属、提示词和 SDK 配置要求由场景策略实现；Provider 保留身份、客户端、初始化与关闭，以及既有接续记忆路由。

| 模块 | 职责 |
|---|---|
| `memory_scene.py` | `MemoryScene` 协议与请求级 `MemorySceneContext`，不依赖具体场景 |
| `memory_scenes.py` | `SCENE_FACTORIES` 注册表，每次创建新的场景实例 |
| `work_memory.py` / `coding_memory.py` | Work/Coding 的场景行为 |
| `memory_tools.py` | 共用工具定义、文档读取与编辑 |
| `agent_memory_provider.py` | 场景委托、身份、SDK/HTTP 客户端及生命周期 |

新增场景时实现 `MemoryScene`，在 `SCENE_FACTORIES` 注册接收项目键的工厂；复用现有行为时可注册已有策略的工厂。实例状态不得存入注册表。若接入 Swarm，还需在其 `jiuwen_rail_builder.py` 注册 Rail 装配工厂；Core 继续决定调用时机。

普通 SDK 顺序为“选择并加载配置 → 按原规则合并 → 应用场景配置要求 → 创建 Runtime”。Work 不追加覆盖，Coding 仅覆盖 `globals.write_document=true`；覆盖在配置副本上完成，保留独立文件配置及 Portal 配置来源。HTTP 不应用本地 SDK 配置。

内联 `config_dict` 按原规则合并 Provider 默认配置；独立 `config_path` 按文件内容交给内核补默认值，不额外补入 Provider 的模型或端点。空内联配置仍可回退配置文件。

未开启接续时，Work/Code 可以同时创建独立 Provider、场景与客户端，并各自关闭。共享文档根仍遵循既有身份、项目过滤和资源锁；不引入全局 Provider 或 Runtime 单例。工具 schema 每次返回独立副本。

普通 Coding 使用默认 `continuity_enabled=false`。显式启用接续后，预取和回写优先走任务接续，
需要调用方传入项目 scope、task_id、binding_id 和 event_id；当前 Coding Rail 不提供这些任务参数。
SDK 接续使用独立配置和共享 Runtime，只注册任务/恢复工具，不应用 Coding 文档配置覆盖。
共享 Runtime 与 Portal 由接续池管理，单个 Provider 退出只释放自身使用权和授权来源；
普通文档模式的资源锁和独立关闭行为保持不变。
