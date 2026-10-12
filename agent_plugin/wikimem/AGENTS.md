# wikimem agent adapter

本目录承接 `rust/wikimem` 中不属于 mem2.0 core API 的 agent 侧能力。

## 文件职责

- `team_sync.py`：团队记忆目录同步 adapter，保留 `SyncState`、pull / push、ETag、checksum、delta、412/413、路径校验和 secret skip 语义。
- `__init__.py`：导出 wikimem agent adapter 的稳定入口。

## 边界

- `team_sync.py` 可以读写本地 team memory 目录，也可以通过 `TeamMemoryRemote` 与远端同步。
- `team_sync.py` 不得被 `MemoryAPI.recall`、`Retriever.retrieve` 或默认 Recaller 自动调用。
- 远端副作用必须显式由 agent adapter、CLI 或评测准备脚本触发。
- 远端 key 写入本地前必须经过相对路径校验，拒绝绝对路径、父级跳转、反斜杠和百分号编码跳转。
- push 前必须跳过潜在 secret 文件，并在 summary 中返回 `skipped_secrets`，不能静默丢弃。
