# 日志敏感信息脱敏

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-03 |
| 影响范围 | `jiuwen_memory/common/log/`、`jiuwen_memory/api/__init__.py`、各层及 Access 已有日志调用点 |
| 测试基线 | `pytest tests/unit`：1868 passed |
| Refs | — |

## 背景

部分运行日志曾直接传入记忆正文、用户作用域、标签、模型原始返回值和 metadata。
这些值可能包含可读的用户信息。单纯提高全局日志级别会同时失去必要的运行状态、计数、
记忆单元 ID 和异常定位信息，因此需要在日志参数进入 Formatter 前统一脱敏。

## 决策

1. `jiuwen_memory.common.log` 提供统一的 `SensitiveDataFilter` 和显式辅助函数：
   `redact_for_log()` 处理正文及任意敏感值，`metadata_for_log()` 保留容器与字段结构但替换
   敏感叶子值，`scope_for_log()` 保留作用域维度名称但隐藏可读身份值。
2. `setup_logging()` 将同一个过滤器安装到 agent-memory 命名空间下的 logger 与 handler，
   对参数化消息、异常文本及外部依赖日志做最后一道防护。
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

## 拒绝的方案

- **全局日志级别调到 CRITICAL**：会让正常错误和运行状态同时消失，无法满足生产排障。
- **把整个 metadata 替换为一个星号**：丢失字段结构，无法判断问题发生在哪个阶段或字段。
- **由治理 Trace 开关控制普通日志脱敏**：会把客户端响应能力与服务端日志安全边界耦合，
  也偏离官方 Trace 接口；因此不采用。

## 验证

- `pytest tests/unit`：1868 passed。
- 单元测试覆盖正文、metadata、Scope 和异常消息的脱敏，同时验证 MemoryUnit 技术 ID 与
  traceback 定位信息能够保留。
- 组件链路测试覆盖 `LLMExtractor` 产生 warning、经 `get_logger()` 安装过滤器并由
  `logging.Formatter` 格式化的完整路径，确认 trailing 原文不会出现在最终日志文本中。
- 静态确认公开 `MemoryAPI.trace()` 与官方最新 `mem2.0` 签名一致，且代码中不存在
  `trace_metadata`、`metadata_trace`、`record_metadata_event()` 等已撤销的 Trace 扩展。

## 已知遗留

- 当前隐私 Filter 对 `get_logger()` 管理的普通运行日志默认全局启用，未提供按 logger
  单独关闭的通道；如后续确有调试需求，应设计独立且受控的诊断机制，不能绕过服务端脱敏边界。
- 本特性只保护 Python logging 路径；第三方组件直接写标准输出、标准错误或独立文件时，
  仍需在对应接入处单独确认。
- `trace`、`inspect`、`get` 等正式 API 返回的数据属于业务响应，不经过日志过滤器；其访问
  控制依赖 API 层鉴权与 Scope 隔离。
