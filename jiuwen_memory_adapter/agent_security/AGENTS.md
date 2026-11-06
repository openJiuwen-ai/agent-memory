# MindVault SDK

MindVault SDK 是 agent 侧安全插件；下层认证、授权与脱敏统称 jiuwen 安全能力。
分发名为 `MindVaultSDK`，导入路径为 `jiuwen_memory_adapter.agent_security`。
当前 PR 仅提供 openJiuwen 适配，保留框架与记忆后端的独立扩展契约。
用户入口为 README.md，接入示例在 examples/；稳定安全契约维护在现有 S10，
设计决策维护在既有 F04。工作记录和逐次验收日志只放本地 security-plans。

## 模块地图

| 文件 | 职责 |
|---|---|
| `__init__.py` | Python 公共门面 |
| `hooks.py` / `config.py` | 统一钩子及独立的框架/后端配置 |
| `_validation.py` | 安全边界的精确类型验证，拒绝容器子类、身份伪装与 bool 冒充整数 |
| `plugin.py` / `managed.py` / `llm.py` 的协作端口 | 显式绑定/恢复可信运行期上下文，只读收据与受控资源登记 |
| `config.py` / `config.example.yaml` | framework 与 memory_backend 独立选型 |
| `frameworks.py` | RuntimeRegistry、运行上下文和窄装配端口 |
| `adapters/_runtime_openjiuwen.py` / `adapters/_native_tools.py` | 原生模型、工具和 ReAct 装配 |
| `adapters/_execution_openjiuwen.py` | 工具最终执行包装与原生审批中断恢复 |
| `adapters/_llm_openjiuwen.py` / `adapters/_delivery_openjiuwen.py` | 模型关联、合作 provider 和原生交付 |
| `plugin.py` / `managed.py` | 安全装配、受控调用和父子 agent 委派 |
| `backends.py` / `backend_jiuwen.py` / `backend_catalog.json` | 后端注册、jiuwen 原生接口与来源信封 |
| `adapters/_memory_tools.py` / `memory.py` | 受控记忆工具与公开 SDK 的最小文本视图 |
| `inspection.py` / `detectors/` | 插件自有检测协议、可替换实现与 PR406 规则快照 |
| `runtime.py` / `execution.py` | 三级策略、载荷检查、单次审批和最终工具门禁 |
| `llm.py` / `llm_http.py` / `llm_response.py` | 实际模型目标、公开 transport 和 JSON/SSE 检查 |
| `sources.py` / `results.py` | 来源账本、执行租约、结果安全视图和流释放 |
| `delivery.py` | 用户响应和 agent 消息的最终交付门禁 |
| `approval.py` / `web.py` / `web/` | 原会话收件箱、ASGI 端口及可挂载控件 |
| `policy.py` / `policy_schema.json` | 版本化目标/动作/边界/等级策略 |
| `audit.py` / `coverage.py` | 无正文有界审计及实际装配覆盖报告 |
| `pyproject.toml` / `LICENSE` | 独立分发，仅强制依赖 regex |
| `README.md` / `examples/openjiuwen_demo.py` | 安装、宿主接入、边界与本地端到端示例 |

## 行为铁律

1. 运行由 RuntimeRegistry 显式选择；当前内置驱动只有 openjiuwen，未知名称不得
   回退。framework 与 memory_backend 可分别配置并注入独立注册表，模型目标与
   外发策略、检测器仍由可信宿主配置，不将上下层实现写死在安全核心中。
2. 统一边界由实际驱动/包装器安装，CoverageReport 只报告实际装配路径。独立的
   静态映射描述和旧前置审批 Rail 留在完整版，不作为执行门禁的替代品。
3. 导入、映射和驱动选型无业务 I/O，不创建 SDK 或安装全局回调。第三方框架只在
   显式工厂中导入。核心不按框架名称分支；驱动通过 RuntimeContext 获得门禁、当前
   可信上下文、模型/工具只读收据及客户端所有权端口，不取得整个 SecurityPlugin。
4. framework 与 memory_backend 独立。后端自带接口名、schema 和实现，不强制统一
   CRUD。当前生产后端只有 jiuwen，目录仅接安全读取；写入/共享/外发不能冒充读取。
   BackendResult 只统一业务值与可信来源快照，非空无来源结果拒绝。
5. 不导入 jiuwen 内核私有模块，不扩展下层检测 API。插件维护 PR406 版本化规则
   快照；检测版本与 SDK 配置独立，不能用未命中推导使用权或外发许可。
6. 主体、权限、Scope、来源及目标来自可信宿主，不能取自模型、工具参数或聊天文字。
   普通 agent 只取得脱敏视图；审批不解密记忆、不授权人类原文查看。固定控件不调用
   LLM、不解析聊天文字为批准；S3 新凭据默认禁用当前载荷原样外发选项。
7. SdkMemoryReader 只使用公开 MemoryAPI。security_for/scope_for/authorize_use
   逐次复核；protected_source_levels_for 必须确认 SDK 对敏感等级提供脱敏视图。
   未确认 S2/S3 和敏感写入 allow 豁免拒绝。detector_version_for 只绑定插件版本；
   上下层规则可不同，来源等级取更严格者。
8. SDK get/list/普通 search 只释放 content/abstract/overview 与带密钥不透明引用，
   不透传元数据、SDK ID、还原链接、异常、附件或轨迹。search 逐项公开 get 复核
   实际 Scope 和修订；未知等级/修订、失效生命周期和不一致视图拒绝。
9. 来源账本只保存引用、等级、版本及摘要，不保存正文。整批验证并登记后才交付。
   发送前复核来源权限、修订、生命周期和投影；发送持有来源租约，其间新记忆交付
   拒绝并要求重试。异步验证在线程执行，取消不得遗留已领取租约。
10. 工具包装在所有前置参数改写后检查。prepare_request 可重复且无外发副作用；
    send 只消费冻结的批准候选，不重读原参数或扩大目标。后端、记忆便捷、委派与
    外部工具共用名称空间，构造成功后才登记。审批绑定主体、会话、操作、目标、
    来源、载荷和策略；超时、关闭、取消、撤权、改写和重放均不得触发发送。
11. 九问普通工具异常可能被 AbilityManager 吞并。包装先返回无业务内容的审批
    占位符，再由 after Rail 交付原生中断；不能当作成功外发。恢复只保存不透明
    引用，原参数保留在有界实例中；兼容框架重放原始参数，从原生审批恢复通道取回
    引用，重跑纯准备后再次复核最终快照。
12. 模型最终检查在 SDK 序列化后的公开 transport。实际 URL、JSON model、provider
    及凭据适用目标须匹配可信目标表；动态选模及 fallback 候选显式装配，每次实际
    重试独立检查和审批。中止阻断同一逻辑调用后续尝试。不修改框架源码或全局客户
    端，不信任远端网关内部路由自述；未知头、目标与重定向拒绝。
13. AFTER 检查结果，原始敏感内容不得进入回调、历史或用户流。文本 Chat SSE 完整
    有界缓冲后跨块检测，不是逐 token 零延迟输出。未闭合、溢出、非法元数据和取消
    不释放部分正文；工具 JSON arguments 解析规范化后检查 Unicode 转义。AFTER
    不降低来源等级，也不把已发生的外部副作用声称为未执行。
14. 完整装配只接收本实例模型和工具，实际对象与结果最终复核；宿主 Rail 优先级
    不得占用最后检查边界。父子双向经过 BEFORE_AGENT_MESSAGE 并继承来源；未知
    旧历史不能补 S1。恢复引用只在当前实例及绑定来源版本内有效。
15. 控件只回传审批引用、版本和选择，不接受替换正文。POST 同源且身份来自宿主
    认证端口；控制入口与异步执行使用同一 Broker，不在 SDK 内抛宿主 checkpoint 中断。
    policy schema_version=1，检查 target/hooks/actions/max_level/allow_original；
    未知版本/字段拒绝。仅替换登记文本并复检，批准不能扩大目标或还原权限。
16. 首 PR 只提供原生异步模型/工具/消息执行路径；检测和本地 SDK 保留同步接口，
    实际使用由异步驱动在线程调用。没有同步 agent/LLM 外发 API；旧记忆便捷入口、
    外部历史恢复和前置审批 API 留在完整版，原生审批续接仍在当前实例内复核。
17. 审计只存固定元数据及带密钥引用，不记录正文、凭据或任意异常；满额/落盘失败
    拒绝释放。发送失败标记 unknown，不承诺外部恰好一次。
18. 关闭先撤销权限，每个拥有资源均尝试清理；普通异常汇总，取消继续传播，重复
    关闭不重复释放。关闭后禁止新建读取器、模型或工具。借用组件依各自所有权关闭，
    不能关闭宿主共享 SDK 或撤回已发生的外部发送。

## 与其他子目录的边界

- 根 wheel 排除本子包，独立 wheel 是插件文件的唯一分发所有者，可与 SDK 并存。
  既有 jiuwenswarm Provider 不因导入本包改变行为。
- 测试镜像在 tests/unit 与 tests/integration 同名路径。真实九问验收使用 Runner/
  ReActAgent、原生 Rail 和中断恢复、实际 SDK、本地 HTTP 模型协议端；模型协议
  替身不替换实际工具、审批、请求或结果交付。
- 测试目录通过完整包名隔离 unit/integration，同一进程全库收集不能共享短名
  agent_security。提交验证包含仓库默认 pytest -m unit，单目录通过不能替代该检查。
- 本仓平台流水线还运行整个 tests；依赖 #406 的 SDK 集成在公开 HumanView 门面
  缺失时明确跳过。能力已存在后的装配和安全失败不得转为 skip；纯插件测试照常运行。
- 当前覆盖本地 SDK 文本读取与文本 Chat Completions；HTTP 记忆、多空间聚合、
  Schema Temporal、附件/多模态及其他模型协议尚未覆盖。
- SourceLedger 默认 128 个上下文（含 retired 墓碑）、4096 个引用；模型/工具墓碑
  有界并保留至 close，不静默淘汰未知旧历史，宿主负责轮换实例。
- 原生受控 ReAct 使用 ainvoke/astream；公开门面只保留装配、策略、检测、审批 UI
  和上下层扩展入口，低层类型从所属模块导入。独立 provider/transport 工厂不等于
  完整 UI 或安全装配已安装。
- ASGI 端口和控件挂入宿主原会话，不提供另一套身份体系。审批/恢复不持久化，
  重启后要求新建受保护会话，不承诺分布式 continuation。
- examples 只用于隔离开发环境，使用公开 SDK 和固定开发身份；不导入测试 fixture，
  不改变生产默认认证。当前 wheel 不打包示例，发布元数据变更另行核对清单。
