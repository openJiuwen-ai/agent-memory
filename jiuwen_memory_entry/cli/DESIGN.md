# CLI 与 MemoryAPI 对齐

最近一次修订日期：2026-09-05

CLI 是 MemoryAPI 的命令行入口，不再兼容 Mem0 风格命令或 legacy payload。
它与 HTTP 使用同一套方法名、参数名、默认值和返回结构，不包含额外业务编排。

## 调用路径

- 本地：命令参数解析/预校验 → 认证上下文 → 共享 JSON 解码 → 同名 MemoryAPI 方法 → 原返回值序列化。
  直接使用 `InProcessClient.call()` 时，从认证开始执行，再由共享契约解码参数。
- 远程：命令参数 → 同名 HTTP URL → 服务端认证与同名 MemoryAPI 方法 → 原 JSON 响应。

本地不经过 HTTP，也不经过 `handler.dispatch`；远程通过
`POST /v1/<method_name>` 调用服务。两者共享 `core/api_contract.py` 的参数定义和
JSON 规则，领域错误共用 `core/error_response.py` 的状态映射和脱敏规则。
`core/handler.py` 与 `legacy_request_adapter.py` 仅留给 MCP 和旧调用方。

## 命令与参数

命令集合直接来自 `MemoryAPI.__abstractmethods__`，当前 36 个公开方法均有同名命令。
参数由方法签名生成，只排除 `self` 和由认证边界注入的 `security`。

- 原样保留参数名，例如 `--unit_id`、`--top_k`、`--continue_on_error`；
- 字符串、枚举和 ISO 8601 时间直接传文本；
- 对象、数组、数字、布尔使用 JSON，例如 `--scope '{"org":"local","user":"developer"}'`、
  `--top_k 3`、`--continue_on_error false`；
- 可空参数接受 `null`；不传可选参数时由 MemoryAPI 原默认值生效；
- `Scope`、`Context`、`MemoryPatch`、`DeleteSelector` 等对象保留原字段和嵌套层级；
- 不保留 `--tenant`、`-u`、`--item-id`、`--k`、`--modality` 等旧别名，不替用户拼装
  patch / selector，也不执行客户端阈值过滤或“先 list 再逐条 delete”等业务流程。

完整参数可用 `scripts/run-cli.sh <method_name> --help` 查看。
当前解析例外：写入 `system_metadata.coords` 的对象值不在 API 声明的 `MetadataValueType`
内，会在 CLI / HTTP 边界被拒绝；该归属判定扩展目前需直接使用 Python API，见
[API F05 已知遗留](../../docs/features/api/F05-http-memory-api-alignment.md#已知遗留)。
`add_async` / `batch_add_async` 等待原协程完成，分别返回原记忆列表和批量结果，
不转换为 job。原本返回任务标识的 `evolve` / `submit_ingest` 保留自身 API 语义。

## 认证与运行

本地默认 `--auth-mode required`。尚未注入 Authenticator 时业务调用返回 503，
不会从 scope 或 payload 生成身份。程序内可通过
`InProcessClient(authenticator=...)` 注入认证器；凭据读取 `AGENT_MEMORY_API_KEY`。

本地功能测试显式使用 `--auth-mode dev`：固定身份为 `local/developer`，
仅跳过凭据校验，不跳过 MemoryAPI 授权。每次调用由受控入口生成独立 request ID，
设置 `Surface.CLI`，请求结束后清理安全上下文；测试身份不能用于生产。

```bash
scripts/run-cli.sh --auth-mode dev add \
  --content 'buy milk' --scope '{"org":"local","user":"developer"}'

scripts/run-cli.sh --auth-mode dev search \
  --query 'milk' --context '{"scope":{"org":"local","user":"developer"}}' --top_k 3
```

默认内存后端不跨 CLI 进程保留数据；上面两条命令是独立调用示例，不共享写入记录。
若要连续验证，使用下节的 batch、持久化后端或远程常驻服务。

远程用 `--server` 或 `AGENT_MEMORY_SERVER` 指定地址；
`AGENT_MEMORY_API_KEY` 作为 Bearer 凭据发送。认证模式由服务器决定，
不能用 CLI 的 `--auth-mode dev` 改变远程服务认证。

```bash
# 仅用于本地功能测试，服务默认绑定 loopback
scripts/run-server.sh --auth-mode dev --port 8137
# 在另一个终端调用
scripts/run-cli.sh --server http://127.0.0.1:8137 add \
  --content 'hello' --scope '{"org":"local","user":"developer"}'
scripts/run-cli.sh --server http://127.0.0.1:8137 list \
  --scope '{"org":"local","user":"developer"}'
```

## 辅助命令与输出

除 API 同名命令外，仅保留两个接入辅助命令：

- `healthz`：本地检查已构建实例；远程调用 `GET /healthz`。
- `batch --input <file|->`：逐行读取 NDJSON，每行是
  `{"op":"<method_name>", ...MemoryAPI参数}`，同一 client 顺序执行。
  它是会话工具，不是 `batch_add` 的别名；不会增加事务或改变 API 批处理语义。
  每条结果独立输出 JSON，失败写 stderr、继续处理后续行，任一失败则非零退出。

```bash
printf '%s\n' \
  '{"op":"add","content":"coffee","scope":{"org":"local","user":"developer"}}' \
  '{"op":"search","query":"coffee","context":{"scope":{"org":"local","user":"developer"}},"top_k":3}' \
  | scripts/run-cli.sh --auth-mode dev batch
```

默认 `--output json` 直接输出 API JSON 原值：数组仍是数组，字符串仍是字符串，
`None` 为 `null`，不再添加 `ok` / `op` / `item` / `hits` envelope，
也不将 `id` 改为 `item_id`。`--pretty` 仅改变缩进。
`text` / `table` / `quiet` 直接读取原结果字段做展示，不改变 client 的返回结构。
batch 固定逐行 JSON，不使用展示选项。

成功写 stdout、退出 0；业务/连接错误写 stderr、退出 1；单命令参数错误退出 2。
客户端内部 `call(method, payload)` 的 `(status, body)` 是执行状态与原响应值的二元组，
不是写入 JSON 响应的业务包装。

## 配置与生命周期

本地用重复 `--config` 叠加 JSON/YAML 层，例如
`--config base.yaml --config local.yaml`，在 OFFLINE 基础上按顺序覆盖。
远程不能同时传本地 `--config`，服务配置由 HTTP 进程负责。
命令结束（含失败）关闭 client，释放内核运行时资源；batch 在全部记录处理完后关闭。

入口脚本 `scripts/run-cli.sh` 将仓库根与 `jiuwen_memory_entry/core` 加入
`PYTHONPATH`，以支持现有 core 平面导入。无需另建 CLI 业务实现。

## 验证

`tests/unit/jiuwen_memory_entry/test_cli.py` 覆盖全部方法/参数集合、默认值省略、
旧字段拒绝、原 JSON 返回、安全身份与目标分离、资源关闭，以及本地/真实 HTTP
模式的增删改查和异步写入。`examples/demo_cli.py` 提供同进程调用示例。
