# Trace 使用说明

| 项 | 值 |
|---|---|
| 日期 | 2026-11-12 |
| 设计归档 | [F11-new-trace.md](../features/common/F11-new-trace.md) |
| 契约 | [S07-common.md](../specs/S07-common.md) |

Trace 使用调用树记录一次请求或后台任务的过程，支持 Factory 自定义插件。
默认 noop，不改变业务响应；选择 memory/memory_log 且有节点时返回通用快照。

## 配置与详细程度

进程内 Config 使用顶层 trace；HTTP/CLI/MCP 启动配置放在 memory_api 段：

```yaml
memory_api:
  globals:
    trace_max_nodes: 128
    trace_max_value_chars: 256
    trace_max_depth: 4
  trace:
    default:
      target: memory
      params:
        level: info
```

| target | 调用树 | 日志 |
|---|---|---|
| noop | 无 | 无 |
| memory | 有 | 无 |
| log | 无 | 有 |
| memory_log | 有 | 有 |

旧 both 注册名已移除，请迁移到 memory_log。

| level | 数据 |
|---|---|
| simple | 关键区间耗时、写入/抽取/召回/融合/披露数量 |
| info（默认） | simple 加阶段、路径、改写状态、策略和降级原因 |
| debug | info 加请求边界、改写详情、抽取条目、召回条件/DSL、候选 ID/分数和结果详情 |

高级别包含低级别。memory 中详情裁剪后返回本次调用方；内置日志 sink 对正文、查询、
过滤值等字段应用隐私标记。凭证不得进入埋点。三项 globals 限制可分别设 null 取消；
限制不覆盖总字节量、容器宽度与日志行数。

请求顶层覆盖示例：

```json
{"extensions": {"trace": "memory:debug"}}
```

也可使用 memory:simple、log:info、memory_log:debug 或 :debug（只改默认级别）。
Access 优先匹配配置中的具名 trace 实例，再解析注册 target；具名覆盖保留完整 params。
context.extensions 不作为该开关。传输方法是否公开顶层 extensions 取决于现有签名，
CLI/MCP 通常使用启动默认。with_trajectory=True 独立控制检索步骤列表，noop/log 也可返回，
不会因此开启通用 trace 响应。超过节点预算时末尾带 trace_truncated。

## 进程内调用

```python
from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.trace import begin_trace, end_trace

register_plugins()
token = begin_trace("memory", level="debug")
try:
    result = api.search(query, context, security=security)
finally:
    snapshot = end_trace(token)
```

begin/end 必须在同一 context 成对执行。返回的 TraceToken 是 opaque 恢复凭据。
直接 MemoryAPI 调用保留业务结果类型，快照由 end_trace 返回；Access 字典响应加 trace，
列表/标量有快照时包装为 result + trace。血缘 trace 动词保持其独立语义。

## 自定义 Factory 插件

应用启动时先 import 自定义模块完成注册，再装配配置：

```python
from jiuwen_memory.common.trace import TraceCollector, TraceProducer, level_param

class CustomerTrace(TraceCollector):
    buffers = True

    def __init__(self, level, sink):
        super().__init__(level)
        self.sink = sink

    def record(self, trace_id, path, data, level):
        # sink 必须线程安全；不要在共享实例保存当前请求数据。
        self.sink.write(trace_id, path, data, level.value)

    def close(self):
        # 按 sink 的所有权协议实现幂等释放。
        self.sink.close()

@TraceProducer.register("customer")
def build(config):
    sink = create_customer_sink(config.params["endpoint"])
    return CustomerTrace(level_param(config), sink)
```

```yaml
trace:
  default:
    target: customer
    params:
      endpoint: configured-by-application
      level: simple
```

支持 Factory build / build_named / dep 的标准参数、具名共享与依赖构造。
进程内可用 begin_trace(collector=已装配实例)，不能同时指定 target。
外部插件由应用 import，不从 YAML 动态导入；不需要继承 Plugin 或扩展 PluginType。
record 接收统一裁剪数据，自定义 sink 负责其日志隐私处理，record 异常当前会传播。

## 埋点

```python
from jiuwen_memory.common.trace import TraceLevel, trace_event, trace_span, trace_annotate

with trace_span("Worker", "run", level=TraceLevel.SIMPLE):
    trace_event("Worker", "result", {"count": len(items)}, level=TraceLevel.SIMPLE)
    trace_event("Worker", "details", lambda: {"items": serialize(items)}, level=TraceLevel.DEBUG)
    trace_annotate({"returned": len(items)}, level=TraceLevel.SIMPLE)
```

traced 可装饰同步/异步方法。简单参数仍在调用前求值，昂贵明细使用 callable，过滤后才执行。
ContextVar 分别保存容器和当前 span；gather/to_thread 自动传播，直接线程池每次提交
分别 copy_context。结束的容器和捕获区间拒绝旧 context 延迟写入。

## 后台与文件日志

```yaml
memory_api:
  globals:
    trace_jobs:
      default: background
      types:
        evolve: evolve_trace
  trace:
    default:
      target: noop
    background:
      target: log
      params:
        level: simple
        log_file: ./logs/jobs.trace.log
    evolve_trace:
      target: memory_log
      params:
        level: debug
        log_file: ./logs/evolve.trace.log
```

选择顺序为任务类型 > 任务默认 > trace.default，显式 noop 不回退。
Job.trace_type 声明稳定类型，内置 evolve/middle_to_long/dreaming，摄入使用 ingest。
实际执行创建独立 ID、容器和预算，记录 Job.run；不继承提交请求的覆盖。
任务快照内部消费/释放，不写回已结束响应，不增加 JobInfo 字段。

专用文件启动时验证可打开，UTF-8 追加；同路径共享 handler，独立文件互不传播。
runtime 等待后台收尾后关闭 sink，失败装配也回滚资源，end_trace 不关闭文件。

## 验证与限制

专项测试位于 tests/unit/common/trace/、tests/unit/api/test_trace_pipeline.py 和
 tests/unit/jiuwen_memory_entry/test_api_contract_trace.py；相关检索、调度、抽取与存储
回归覆盖行为兼容、并发预算和资源收尾。详细基线与已知限制统一维护在 F11。
