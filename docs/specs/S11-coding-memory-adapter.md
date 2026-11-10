# Coding 文档记忆适配契约

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `jiuwen_memory_adapter/jiuwenswarm/` |
| 最近一次修订日期 | 2026-11-11 |
| 关联特性 | [F08](../features/F08-coding-document-memory.md) |

## 范围与不变量

1. `AgentMemoryMemoryProvider` 默认 `scene="agent"`，原工作模式工具保持原名、入参及语义。
2. `scene="code"` 使用宿主提供的 `project`，缺省为 `default`；模型不能覆盖 project 或 Scope。
3. SDK 由宿主注入可信 `security_provider`；HTTP 沿用服务端身份校验。适配层不增加内核公开接口。
4. Code 与单人 Work 共用文档根；宿主以运行 Agent 名称优先绑定身份，缺省回落配置 agent_id。SDK 中 org/space/user 相同且 Agent 同名时共享画像，不同 Agent 名称按 Scope 查询隔离；USER.md 的共用物理路径不取消身份过滤。项目事实按 project 区分。Team 保持原身份规则。
5. Code SDK 在选定配置来源后仅强制 `globals.write_document=true`：`config_dict` 继续合并 Provider 默认装配；`config_path` 按文件原样交给内核补默认值，不额外合并 Provider 的模型或端点。HTTP 服务须自行开启文档记忆。

## Provider 参数

在现有关键字参数后新增：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `scene` | `agent` | `agent` 或 `code`；非法值抛 ValueError |
| `project` | `default` | Code 项目目录键，禁止空值、点目录、路径分隔符及 NUL；Work 忽略 |

身份通过既有 `initialize(user_id, scope_id, agent_id, session_id)` 绑定；Code 使用宿主解析后的运行 Agent 名称，其余维度沿用配置。session 默认空字符串，显式值原样保留，不自动绑定聊天会话 ID。文档读取匹配 session；现有影子索引搜索只严格匹配 org/space/user/agent，跨 session 召回。HTTP 继续沿用既有认证身份映射。

## Code 工具

统一前缀 `agent_memory_coding_memory_`。不暴露 project 参数；额外传入也不能改变绑定。

| 后缀 | 参数 | 行为 |
|---|---|---|
| `search` | `query`；`top_k=5`（1～50） | `system_metadata.project == 当前项目`，不混入空项目 |
| `write` | `content` | TEXT + `infer=true`，写入事实并由现有抽取器分类；非整文件覆盖 |
| `read` | `md_filename`；`offset`、`limit` | USER.md、MEMORY.md 或 YYYY-MM-DD.md；行号从 1 开始 |
| `edit` | `md_filename`、`old_text`、`new_text` | 复用现有唯一文本替换；零次或多次匹配报错 |

USER.md 固定读取空 project；其他文件固定当前项目。write 失败返回 error，抽取为空或去重返回 `stored=false`，有新单元才返回 `stored=true`。Code 不接受旧工作模式工具别名。

## 召回与写入

`prefetch(query, top_k=5, background=False)` 并行读取 USER.md 和项目 MEMORY.md，普通请求同时搜索项目。优先使用项目搜索结果；为空、失败或超过 5 秒时使用已读取的 MEMORY.md。背景请求只读文档。读文档本身失败则对应部分为空。

`sync_turn(user, assistant)` 两者均非空时把本轮拼接为 TEXT，`infer=true`，绑定当前项目。是否调用、后台排队和资源关闭由 core Rail 决定。

分类复用现有 `user_profile / long_term_memory / daily_memory`，并非模型可选文件路径。

```text
{root}/USER.md
{root}/memory/{project}/MEMORY.md
{root}/memory/{project}/daily_memory/YYYY-MM-DD.md
{root}/memory/.shadow/shadow.db
```

MEMORY.md 保存正文块，不是旧 Coding 的主题索引。看门狗、编辑后索引更新、检索授权沿用文档记忆内核。同进程多 Runtime 的文档修改与完整对账按根目录、索引资源协调：共享任一资源串行，两者独立可并行，补偿结束后才释放；Provider 生命周期与身份隔离不变。跨进程协调、历史主题文件迁移和总 token 预算不在此契约内。


## HTTP 连接与身份边界

HTTP 客户端只访问配置的服务；不装配本地 Runtime，不启动本地看门狗，不传递 SDK 的
config_dict、文档根或模型配置。服务自行启用文档写入、抽取、影子索引及看门狗。
四个工具使用既有 `/v1/read_md_file`、`/v1/add`、`/v1/edit_md_file`、`/v1/search`；
sync_turn 使用 `/v1/add`，失败不代表已保存。重启客户端不上传本地文件。

沿用原 HTTP 映射：org=identity_org，user=identity_user，agent=运行名称，
session=user_id，space=""；scope_id/原 session_id 不透传。不能由 SDK 的五维直映射
推导 HTTP 的隔离；同一认证用户下仅修改 user_id 不保证查询隔离（影子搜索跨 session）。
认证身份、user_id 与 Agent 名称一致时，Work/Code 读取同一画像；项目过滤规则不变。
