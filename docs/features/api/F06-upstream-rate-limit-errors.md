# F06 — 上游模型限流错误的可重试响应

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-09 |
| 影响范围 | HTTP / CLI 错误适配、`docs/specs/S02-memory-api.md` |
| 测试基线 | 接入层单测 353 passed、1 skipped；修改的 Python 文件 Ruff 与编译检查通过 |
| Refs | #431（GitCode #241） |

## 背景

模型供应商返回 429 时，OpenAI SDK 抛出 `RateLimitError`，其名称不同于领域层的
`RateLimitedError`。仅识别领域异常会把已经耗尽 SDK 重试的限流错误变成不可重试的
500。HTTP / CLI 的未知异常分支还会在共享映射前将其改写为 `InternalError`，
因此只补映射表仍无法修复真实接口响应。

## 决策

HTTP / CLI 显式识别 SDK 限流异常，并保留其类型交给共享错误映射。上游与领域限流
沿用同一组 429、可重试标记和固定脱敏文案；HTTP 同时返回现有的 `Retry-After: 1`。
错误名分别保留 `RateLimitError` 与 `RateLimitedError`，与已有保留异常类名的契约一致。

## 拒绝的方案

- 将领域异常重命名为 SDK 名称：会破坏已有调用方与入口限流器的兼容性。
- 只增加错误名映射：真实 HTTP / CLI 会提前丢失 SDK 异常类型，无法到达该映射。
- 将所有 SDK 异常交给映射：超出本次限流修复范围，其他未知异常继续返回通用 500。
- 顺带增加模型重试：会改变请求时延和调用次数，需要独立确定超时与退避预算。

## 验证

- 使用真实 OpenAI SDK 异常对象，离线验证类名、类型和实例三种映射入口；保留领域限流
  与未知 SDK 错误行为的回归测试。
- 使用本机 HTTP 测试服务器验证 `add`、`search` 的状态、响应体、请求 ID 和
  `Retry-After`，另验证本地 CLI 的 `search` 错误响应与认证上下文清理。
- 回归测试在修复前稳定复现 500；测试不调用模型供应商，不依赖真实配额耗尽。
- Python 3.11.15、OpenAI SDK 3.27.0 下运行
  `PYTHONPATH=.:jiuwen_memory_entry/core pytest tests/unit/jiuwen_memory_entry`，
  353 项通过；未安装可选 `mcp` extra，MCP 测试模块跳过。修改的 Python 文件通过
  `ruff check`，生产代码通过 `compileall`；未运行真实供应商长稳测试。

## 已知遗留

供应商 RPS / 配额限流的客户端重试和退避策略不在本次范围。可重试标记表示调用方可按
响应退避，不保证下一次请求成功，也不透传供应商特有的等待时间。
