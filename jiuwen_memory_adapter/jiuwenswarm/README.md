# JiuwenSwarm 记忆适配

`AgentMemoryMemoryProvider` 提供 SDK/HTTP 两种连接方式。工作模式使用默认 `scene="agent"`；Coding 使用 `scene="code", project="宿主生成的项目键"`，搭配 core 的 `JiuwenMemoryCodingRail`。

- 参数、工具和数据边界：[Coding 适配契约](../../docs/specs/S11-coding-memory-adapter.md)。
- 实施决策：[Coding 文档记忆](../../docs/features/F08-coding-document-memory.md)。
- 调用时机、开关、项目键和旧 AutoMemory 互斥由 Swarm 宿主负责。

SDK 必须注入可信 `security_provider`，文档目录由 `markdown_store.default.params.root` 指定。HTTP 必须使用已启用文档记忆、可正常认证的服务。两种方式都复用原 MemoryAPI，不新增 search/evolve 平行接口。

Work/Code 的记忆 Agent 身份都优先采用运行名称，再回落配置 agent_id。SDK 中，相同 org/user/space 和 Agent 名称可跨模式共享 USER.md；不同 Agent 名称仍按 Scope 查询隔离，项目记忆按 project 筛选。session 沿用配置（默认空字符串），不强制覆盖；共用文档根不会合并身份。


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
