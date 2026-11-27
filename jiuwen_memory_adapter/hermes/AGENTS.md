# Hermes adapter

## 模块地图

- `provider.py`：`JiuwenMemoryProvider`，只走本机服务的 hook HTTP 端点。
- `plugin.yaml`：Hermes 插件元数据，provider 名为 `jiuwen_memory`。

## 行为铁律

- 只读/写本机服务，不直接访问内核存储；失败返回空/None，不中断宿主。
- 轮次 ID 由 prefetch 生成并在 sync_turn 复用；没有会话 ID 时也不抛异常。
- prefetch 返回的注入文本包在 `<agent-memory-context>…</agent-memory-context>` 中，条目正文里的标记字面量先中和（S11「召回注入标记」）；无条目时返回空串。
- `on_memory_write` 只转发到 `/local/sync/mirror`，失败静默；镜像不创建独立安装，也不在适配器内判重。
- 安装、替换 provider 与卸载由 `jiuwen_memory_entry.local.hosts.hermes` 负责，插件目录本身不写宿主配置。
- MCP 配置同时接受 `mcp_servers` 与 `mcpServers` 两种键；按 profile 安装时只改该 profile 的配置，不覆盖用户原有 provider。
- 会话历史导入格式未在选定 Hermes 版本核实，能力表把 `session_import` 标为 `unverified`；`sync grant` 在能力未核实前拒绝显式会话导入。
