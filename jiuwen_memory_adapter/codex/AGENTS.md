# Codex 记忆 Adapter

## 模块地图

| 文件 | 职责 |
|---|---|
| hooks.py | Codex hook 载荷解析、注入文本渲染、输出结构、1 MiB 读取上限 |

## 行为铁律

- 只解析事件，不做记忆决策：recall/capture 全部交给本机服务 `/local/hook/*`。
- 缺 `session_id`、缺 `turn_id`（Stop）、空 prompt、未知事件、超大或非法 JSON 都是退出 2 的静默 no-op，不打断该轮。
- 注入文本只含服务返回的记忆条目与提示；已见记忆以 `<!-- agent-memory: ids -->` 追加，供宿主去重。
- 不读取凭证、不拼接 URL、不落盘；认证与发现由 CLI 的 `hook` 子命令负责。

## 与其他子目录的边界

CLI（`jiuwen_memory_entry/local/cli.py`）负责安装解析、发现与 HTTP；本目录不导入 `jiuwen_memory` 之外的内核模块，也不实现重试或写入判定。
