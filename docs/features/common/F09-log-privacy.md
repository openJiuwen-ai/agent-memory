# 日志敏感信息脱敏

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-03 |
| 影响范围 | `jiuwen_memory/common/log/`、`jiuwen_memory/api/__init__.py`、各层及 Access 已有日志调用点 |
| 测试基线 | SDK 日志专项 75 项通过；全量单测见「验证」 |
| Refs | #277 |

## 背景

部分运行日志曾直接传入记忆正文、用户作用域、标签、模型原始返回值和 metadata。
这些值可能包含可读的用户信息。单纯提高全局日志级别会同时失去必要的运行状态、计数、
记忆单元 ID 和异常定位信息，因此需要在日志参数进入 Formatter 前统一脱敏。

## 决策

1. `jiuwen_memory.common.log` 提供统一的 `SensitiveDataFilter` 和显式辅助函数：
   `redact_for_log()` 处理正文及任意敏感值，`metadata_for_log()` 保留容器与字段结构但替换
   敏感叶子值，`scope_for_log()` 保留作用域维度名称但隐藏可读身份值。
2. `get_logger()` 会为日志器添加脱敏过滤器（`SensitiveDataFilter`）。初始化日志后，SDK
   会接收名称以 `agent_memory.` 或 `jiuwen_memory.` 开头的日志，先脱敏，再交给终端、文件
   或业务日志处理器。每个输出会拿到一份单独的日志记录，其中的字典、列表等标准容器也会
   复制，避免业务处理器修改记录时影响另一边的输出。自定义业务对象和异常信息仍可能共用，
   具体复制范围见 [S02 日志接管说明](../../specs/S02-memory-api.md#sdk-运行日志接管)。
   应用此前手工添加的日志处理器会保留，仍按 Python logging 的调用顺序接收日志。业务怎样
   接收日志、何时关闭处理器，见 [F09 SDK 运行日志接管](../api/F09-sdk-runtime-logging.md)。
3. 记忆单元 ID、任务 ID、调用链 ID、数量、耗时、状态和固定枚举等诊断字段可以保留；
   `user`、`session`、正文、标签、关键词、实体、模型响应和 metadata 叶子值不得明文进入
   普通服务端日志。
4. 异常日志保留异常类型和 traceback 的文件、行号、调用栈，只隐藏异常消息中的值。
5. 本特性不改变业务对象、持久化数据或正式 API 响应。治理 `trace` 接口完全沿用官方行为，
   不增加披露开关、额外响应字段或跨模块采集器。
6. Access 受依赖边界约束，只能导入 `jiuwen_memory.api`。因此 API 包仅重导出
   `install_privacy_filter()`、`metadata_for_log()`、`redact_for_log()` 和
   `scope_for_log()` 四个日志辅助函数；这属于 Python 公共日志能力，不增加 HTTP 字段，
   也不改变 `MemoryAPI` 的业务契约。

7. 脱敏时若读取自定义容器等操作抛出普通异常，拒绝整条日志，不把可能尚未脱敏的内容
   交给 SDK 管理的输出。只向 `stderr` 输出安全诊断，不包含正文、异常值或源码内容；提示失败也不
   向业务抛错。日志器上的 Filter 和 SDK 分发器都执行这条约定，后续正常日志继续处理。

## 拒绝的方案

- **脱敏失败后继续打印原记录**：无法保证记录安全，可能把敏感内容交给业务或写入文件，
  所以该条记录必须丢弃。
- **全局日志级别调到 CRITICAL**：会让正常错误和运行状态同时消失，无法满足生产排障。
- **把整个 metadata 替换为一个星号**：丢失字段结构，无法判断问题发生在哪个阶段或字段。
- **由治理 Trace 开关控制普通日志脱敏**：会把客户端响应能力与服务端日志安全边界耦合，
  也偏离官方 Trace 接口；因此不采用。

## 验证

- `pytest tests/unit`：全量单测通过，13 项因依赖、平台或外部环境跳过。
- 单元测试覆盖正文、metadata、Scope 和异常消息的脱敏，同时验证 MemoryUnit 技术 ID 与
  traceback 定位信息能够保留。
- 组件链路测试覆盖 `LLMExtractor` 产生 warning、经 `get_logger()` 安装过滤器并由
  `logging.Formatter` 格式化的完整路径，确认 trailing 原文不会出现在最终日志文本中。
- 静态确认公开 `MemoryAPI.trace()` 与官方最新 `mem2.0` 签名一致，且代码中不存在
  `trace_metadata`、`metadata_trace`、`record_metadata_event()` 等已撤销的 Trace 扩展。

对两种 SDK 日志器名称补充异常容器回归，验证读取失败不会打断调用、未脱敏记录不落盘、
诊断不泄漏敏感值，以及故障后正常记录仍可输出。

## 已知遗留

- 当前隐私 Filter 对 `get_logger()` 管理的普通运行日志默认全局启用，未提供按 logger
  单独关闭的通道；如后续确有调试需求，应设计独立且受控的诊断机制，不能绕过服务端脱敏边界。
- 本特性只保护 Python logging 路径；第三方组件直接写标准输出、标准错误或独立文件时，
  仍需在对应接入处单独确认。SDK 不改变第三方库原有的日志输出方式。
- `trace`、`inspect`、`get` 等正式 API 返回的数据属于业务响应，不经过日志过滤器；其访问
  控制依赖 API 层鉴权与 Scope 隔离。
