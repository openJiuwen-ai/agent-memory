# Trace 横切插件

规约见 [S07](../../../docs/specs/S07-common.md)，设计与取舍见
[F11](../../../docs/features/common/F11-new-trace.md)。

## 模块地图

| 文件 | 职责 |
|---|---|
| trace.py | TraceCollector / TraceProducer、noop_collector 禁用实例接口、三级过滤、ContextVar 容器/当前 span、执行预算、局部捕获 |
| runtime.py | trace 具名实例装配、API/执行器绑定、任务策略选择与独立作用域 |
| trace_impl/ | noop_trace.py / memory_trace.py / log_trace.py / memory_log_trace.py 内置注册 |
| __init__.py | 对业务与自定义插件公开的接口导出 |

## 行为铁律

- 请求数据只存在于执行容器，插件实例可持有配置与线程安全 sink 资源，不能保存当前请求状态。
- 详细程度按 simple < info < debug 过滤；昂贵 debug data 使用惰性 callable，过滤后才求值。
- 容器和当前 span 分别用 ContextVar；直接线程池每次提交单独 copy_context，不共享可变栈。
- 局部捕获不关闭外层作用域、不提升通用响应策略；投影字段独立于格式化 data。
- 所有分支共用执行预算；关闭容器及局部捕获后不接收延迟写入。
- record 收到统一裁剪数据；内置日志对正文/查询字段加隐私标记，内存快照保留裁剪后的详情。
- log_file 只来自启动参数；共享 handler 按路径引用释放，end_trace 不释放 sink。
- 自定义 Factory 插件由应用主动 import；禁止根据 YAML 动态导入任意 Python 类。
- target 缓存通过 Factory.registration_key 比较 builder 身份，不直接读取 Factory 注册表。
- 未绑定 runtime 的后台执行器使用 noop_collector() 屏蔽继承作用域；该接口不读取进程默认配置、不依赖插件注册，私有实现仅在 trace.py 内使用。

## 与其他子目录的边界

common 不依赖 retrieval DTO；轨迹投影由 retrieval/trace_projection.py 完成。
API 装配根绑定默认实例与资源生命周期；Access 只从 jiuwen_memory.api 取公开入口。
后台执行器在实际运行入口建立作用域，不能复用在线请求的容器或覆盖值。
