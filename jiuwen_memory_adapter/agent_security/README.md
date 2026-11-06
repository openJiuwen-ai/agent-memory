# MindVault SDK

MindVault SDK 是可独立分发的 agent 侧安全插件。在 openJiuwen 智能体与 jiuwen memory
之间检查敏感记忆的使用，并在模型请求、工具执行和 agent 消息实际发出前执行外发管控。
下层认证、授权、脱敏和人类原文查看统称 **jiuwen 安全能力**。

## 支持范围

| 项目 | 当前范围 |
|---|---|
| 智能体框架 | openJiuwen；原生 `Runner`、`ReActAgent`、Rail 与异步调用 |
| 记忆后端 | jiuwen memory 本地公开 SDK；`get`、`list`、普通 `search` 的安全文本视图 |
| 模型协议 | OpenAI Chat Completions 文本 JSON/SSE |
| 主要出口 | LLM 请求、受控工具、同一插件管理的 subagent/其他 agent 双向消息 |
| 审批 | 同一用户会话中的固定控件或宿主控制入口；选择不经过 LLM 解释 |
| 扩展 | 框架、记忆后端独立注册；检测器和外发策略可替换 |

只覆盖显式通过插件装配的对象。直接创建的模型客户端、未包装的工具或私有 agent RPC
不会自动被接管；这不是对任意宿主代码的网络沙箱。LangChain、OpenClaw 留给后续适配。

## 安装和兼容基线

在仓库根目录使用同一 Python 环境安装 SDK 与插件：

```bash
python -m pip install -e ".[security]"
python -m pip install -e ./jiuwen_memory_adapter/agent_security
```

根 `JiuwenMemory` wheel 排除本插件目录；只安装根包不会安装 `MindVaultSDK`。
插件自身只强制依赖 `regex>=2024.11.6`，导入和配置解析不要求框架或记忆 SDK 已安装。
实际运行还需要 openJiuwen 及其模型客户端依赖，浏览器宿主演示另需 ASGI server。

当前验收覆盖 agent-core 源码版本 `0.1.15` 和公开分发版本 `openjiuwen==0.1.20.post1`，
使用 Python `3.13.9`。框架要求 Python
`>=3.11,<3.14`；插件声明 Python `>=3.11`。实际组合运行应满足两者及 SDK 的交集。
源码基线不表示任意名为 `openjiuwen==0.1.15` 的分发物都具有相同接口。
安装经核对的 agent-core checkout，并固定其源码 revision：

```bash
python -m pip install -e /path/to/agent-core
```

下层须具有公开的 `RequestSecurityContext`、`Scope`、`MemoryAPI.get/list/search`，且普通
读取提供 `privacy_level`、`privacy_revision` 和脱敏视图。当前验收基于本仓库已有的下层
安全实现（[PR #406](https://gitcode.com/openJiuwen/agent-memory/pull/406)）；
完整记忆闭环须先具备该 PR 的下层能力，不能直接改用不具备能力的旧 SDK。插件不新增 memory 检测 API，也不导入 SDK 私有检测模块。

模型客户端使用框架环境中的 `openai`，最终 transport 支持 `httpx`，以及安装了相应
OpenAI 客户端时的 `httpx2`。验收以本地 HTTP 模型协议端验证实际请求，不依赖商业 LLM
生成质量；不能据此声称全部远端 provider、网关或客户端版本兼容。

本页示例复验的客户端组合为 `openai 3.46.0`、`httpx 0.28.1`、`httpx2 2.16.0`、
`regex 2026.9.29`，用于记录已验证组合，不是兼容版本上下限。

部署与复现时记录环境版本和源码 revision：

```bash
python -c "import sys, importlib.metadata as m; print(sys.version); print({n:m.version(n) for n in ('openai','httpx','regex')})"
git -C /path/to/agent-core rev-parse HEAD
git rev-parse HEAD
```

SDK 同步读取与检测内部在线程运行；宿主负责提供适合在线程中调用的可信端口。
自定义异步宿主也应在线程调用同步 SDK 入口，不能在事件循环内嵌套执行同步 `add()`。

## 最小端到端示例

[examples/openjiuwen_demo.py](examples/openjiuwen_demo.py) 连接真实 SDK 和框架，用虚构电话
创建临时记忆，装配模型、记忆工具、报告工具和子 agent，在同一终端会话里确认审批。
这只是本地接入示例：报告工具只写本进程内存，不向外部业务系统发送报告。

```powershell
$env:MINDVAULT_DEMO_API_KEY = "你的模型凭据"
python -m jiuwen_memory_adapter.agent_security.examples.openjiuwen_demo --endpoint https://your-host/v1/chat/completions --model your-model --provider your-provider
```

仅在隔离开发环境运行，示例使用固定 `local/developer` 开发身份和临时加密存储；不要输入
真实个人信息或凭据。终端中的“继续外发”仍会让当前候选发往指定模型，需核对目标。
生产集成替换开发认证器、授权函数、密钥和存储，不复制演示身份设置。
模型是否选择每个工具由模型响应决定；完整边界验收应使用后文的确定性集成测试。
示例随仓库源码提供，当前独立 wheel 不包含 `examples/`。

## 宿主装配

高层路径依次为：

1. 宿主认证用户，建立可信 `Actor`，调用 `plugin.new_session(actor)`；保存返回的会话
   对象用于运行与审批，不能继续使用传入的旧会话 ID。
2. `SecurityPlugin(...)` 接收框架/后端选型、外发策略、实际模型目标和授权端口。
3. `connect_memory_backend(...)` 连接 SDK；`memory_tools(backend)` 创建该后端自己的工具。
4. `model(...)`、`tool(...)` 创建受控对象；`await agent(...)` 安装框架原生接线。
5. 调用 `await agent.ainvoke(text, session=session)` 或 `agent.astream(...)`。
   父 agent 使用 `delegate_tool(child, name=...)` 委派，双方必须属于同一插件实例。
6. 宿主将审批提交给当前用户会话，最后 `await plugin.aclose()`；SDK 由其所有者关闭。

只解析配置不会安装门禁；高层装配会安装必要 Rail 和包装，宿主不应重复手工安装。
在创建原生 agent 前先启动 `Runner`，退出时先清理插件，再停止 `Runner`。

| 必需端口 | 调用语义与责任 |
|---|---|
| `authorize(operation)` | 每次外发及复核判断当前主体、目标、动作是否允许，必须返回 `True` 才允许 |
| `authorize_input(actor, hook, producer)` | 判断模型/工具/agent 结果能否进入该会话，并支持来源再次复核 |
| `authorize_transfer(sender, recipient, snapshot)` | agent 间传递权限；批准内容不能替代接收方授权 |
| `security_for(actor)` | 返回公开 SDK 的可信 `RequestSecurityContext`，与当前 actor 匹配 |
| `scope_for(actor)` | 返回由宿主限定的业务 `Scope`，不从模型参数中接受身份或租户 |
| `authorize_use(actor, reference, purpose)` | 初次读取和后续外发前复核来源的用途/使用权限 |
| `protected_source_levels_for(actor)` | 宿主确认此 SDK 配置确实为哪些等级提供脱敏视图；S2/S3 必须明确确认 |

`detector_version_for(actor)` 可选，仅绑定插件检测版本，不证明插件与 SDK 规则相同。
`notify(owner, event)` 可选，用来唤醒原会话 UI；不应自动批准，也不让模型解释通知。
`audit_sink(event)` 可选，接收固定审计元数据。宿主须定期消费 `plugin.audit.drain()`；
设置 sink 不会自动排空内存队列。审计不可用或容量满时拒绝继续释放。
openJiuwen 结果授权会收到模型 target ID、`framework-model-result`、工具名以及
`agent:<child_id>` 等 producer；最小示例展示显式允许集合，不应一律返回 `True`。

## 框架、后端与策略配置

宿主使用自己的 YAML/配置系统加载 [config.example.yaml](config.example.yaml)，分别传入：

```python
plugin = SecurityPlugin(
    settings["agent_security"],
    policy=settings["policy"],
    targets=trusted_model_targets,
    authorize=authorize_egress,
    authorize_input=authorize_result,
    authorize_transfer=authorize_agent_transfer,
)
```

这里的变量是宿主已认证和配置好的端口；完整演示见上面的示例。`framework` 与
`memory_backend` 分别选型，不要求其他记忆系统模仿 jiuwen 的 CRUD 接口。
当前内置值分别为 `openjiuwen` 和 `jiuwen`，未知名称拒绝，不静默回退。

每个 `LlmTarget(target_id, provider, model, endpoint)` 由可信宿主给出，`endpoint` 是完整
`/chat/completions` URL。provider 是本地可信配置标签，实际放行还需要 URL、请求体
model 和凭据适用目标匹配。无法核验远端网关背后的内部路由。

| 策略字段 | 语义 |
|---|---|
| `schema_version` | 当前数据契约为 `1`，不同于管理员的 `version` 修订标签 |
| `targets[].target` | 模型 target ID、工具登记目标、`current-user` 或 `agent:<agent_id>` |
| `hooks` | 可选，默认四个外发 BEFORE 边界；可缩小到所需出口 |
| `actions` | 可选，默认 `["*"]`；工具使用工具名，其他典型值为 `llm.chat.completions`、`agent.message`、`agent.response` |
| `max_level` | 可选，默认 `S3`；同时限制可信来源等级和本次载荷检测等级 |
| `allow_original` | 可选，默认 `true`；设为 `false` 移除敏感审批的原样发送选择 |

建议显式配置各字段；完整结构见 [policy_schema.json](policy_schema.json)。未登记目标、
未知字段和无可发送候选均拒绝。S1 且未改变的载荷在来源也是 S1 时可自动放行；接触
敏感来源后，即使文字未再次命中规则，也不会自动当成无敏感来源的数据。
新检测到 S3 的当前载荷不提供原样发送选项，即使 `allow_original=true`。

`jiuwen` 后端当前暴露 `jiuwen_memory_get(unit_id)`、`jiuwen_memory_list(limit=20)`、
`jiuwen_memory_search(query, top_k=10)`。只向 agent 释放 `reference`、`content`、
`abstract`、`overview`；不透传真实 SDK ID、元数据、还原链接、附件和轨迹。
`reference` 是本实例的不透明引用，不能当成 SDK ID 再传给 get。

## 实际安全边界

| 统一钩子 | 安装后的语义 |
|---|---|
| `BEFORE_MEMORY_EXPORT` | SDK 返回值进入 agent 前，校验认证/Scope、来源等级、修订和脱敏视图 |
| `BEFORE_LLM_CALL` | 序列化后的 HTTP 请求发出前检查实际目标和载荷；每次重试重新检查 |
| `AFTER_LLM_CALL` | 模型结果进入框架前检查，工具 arguments 的 JSON 转义先规范化 |
| `ON_LLM_STREAM_CHUNK` | 属于模型调用的流检查边界；当前完整、有界地缓冲 SSE 后跨块检查再释放 |
| `BEFORE_TOOL_CALL` | 所有前置改写及 `prepare_request` 完成后检查并冻结最终请求 |
| `AFTER_TOOL_CALL` | 工具结果进入框架前检查；不能撤销已经发生的外部副作用 |
| `BEFORE_AGENT_MESSAGE` | 父子双向消息交付前检查与授权，接收方执行前继承可信来源 |
| `BEFORE_AGENT_RESPONSE` | 最终用户响应交付前检查 |

这些名字是插件协议，并不要求框架原生 hook 同名。`plugin.coverage().public()` 仅描述
当前实例实际装配的路径与缺口；只有配置或独立 transport 不代表完整框架已受控。

openJiuwen 的 `before_model_call` / `after_model_call` Rail 关联模型作用域及复核
框架投影，真正的 LLM 请求与响应门禁位于合作 provider 的 HTTP transport。
`before_tool_call` / `after_tool_call` Rail 负责工具关联和原生中断，最终执行体仍在
参数准备后检查。agent 消息与用户响应通过受控委派和 `Runner.run_agent` /
`Runner.run_agent_streaming` 的最终交付包装实现，不假设框架另有同名消息 hook。

工具的 `prepare_request(payload, context)` 必须纯准备、可重复且没有外发副作用。
`send(approved, context)` 只消费已冻结的批准请求，不得重读旧参数、另换目标或添加未检查
内容；插件不能证明任意工具内部代码遵守这些要求。消息/工具的宿主身份来自回调关联，
不能由工具参数声明。动态模型候选必须全部通过插件登记；未知候选、未知头和重定向拒绝。

## 在原会话界面审批

宿主挂载同源 ASGI app，复用原会话认证，回传 `new_session()` 创建并由宿主保存的 owner：

```python
from jiuwen_memory_adapter.agent_security import create_approval_app

approval_app = create_approval_app(
    plugin,
    authenticate=authenticated_session_for_asgi_scope,
    allowed_origins=["https://your-agent.example"],
)
# 用宿主 ASGI router 挂载到 /security；认证逻辑属于宿主，不从请求正文构造 Actor。
```

在原聊天页面加入：

```html
<script type="module" src="/security/widget.mjs"></script>
<jiuwen-security-approvals endpoint="/security"></jiuwen-security-approvals>
```

审批端口相对于挂载路径提供 `GET /requests`、`POST /decide`、`GET /audit` 和
`GET /widget.mjs`。POST 只接受同源 JSON，身份仍须通过宿主验证；前端仅提交
`approval_id`、`version`、`choice`，不能指定 actor、目的地或替换正文。

选择值为 `CONTINUE_ORIGINAL`、`ABORT`、`SEND_REDACTED`。固定模板直接呈现，
不额外调用 LLM。策略可能移除某些选项；脱敏候选由插件生成并复检，实际执行用该候选。
“继续外发”指当前 agent 已持有的候选，不会解密或还原 SDK 隐藏的记忆原文。
替换只发生在登记的文本字段（如 LLM messages、工具或消息 body），不自动改写
URL、身份、凭据或非文本参数；无法构造安全候选时不提供“脱敏外发”。

没有浏览器的应用可以由可信会话调用 `plugin.pending(owner)` / `plugin.decide(...)`，
但不能通过解析“同意”等聊天文字生成授权。`create_chat_app(...)` 是可选最小宿主，
提供聊天页与运行轮询；生产应用通常只挂载审批 app 和控件。

## 扩展与运行限制

- 上层用 `RuntimeRegistry.register(name, factory)` 显式登记可信驱动，再通过
  `runtime_registry=` 注入；接口见 `RuntimeDriver`。驱动必须证明最终执行/交付覆盖，
  通过接口形状校验并不等于完成安全验收。
- 下层用 `MemoryBackendRegistry.register(name, factory)`，通过 `backend_registry=`
  注入。后端自带 `BackendOperation` 目录、参数 schema 和实现；`BackendResult` 统一
  业务值和已登记的 `SourceSnapshot`，当前只接受 `memory-read`，不把写入冒充读取。
- 检测器实现 `TextInspector.version` / `inspect(text, label=...) -> TextInspection`，
  通过 `inspector=` 注入；或用 `create_text_inspector(...)` 配置规则。检测失败拒绝释放，
  未命中不等于有权限；上下层规则不同不允许降低已知来源等级。
- 仅文本/代码记忆，未覆盖附件、多模态、HTTP memory、跨空间聚合、其他模型协议。
  SSE 不是逐 token 即时输出；超限、截断或取消不释放未完成检查的部分正文。
- 审批、原生续接及来源账本是实例内状态。重启后必须新建受保护会话；不支持外部
  历史导入、持久化审批和分布式恢复。未知历史不能补成 S1。
- 来源、审计和审批容器均有界，容量满时拒绝；宿主应消费审计并轮换实例。
  关闭不撤回已完成的外发，发送失败不承诺外部恰好一次执行。

## 验证与构建

仓库根安装开发依赖后执行纯测试和静态检查：

```bash
python -m pip install -e ".[dev,security]"
python -m pip install -e ./jiuwen_memory_adapter/agent_security
python -m pytest -m unit
ruff check jiuwen_memory_adapter/agent_security tests/unit/jiuwen_memory_adapter/agent_security tests/integration/jiuwen_memory_adapter/agent_security
```

上面的仓库默认入口先收集全部测试，再选择 unit；插件测试使用完整包名隔离 unit 和
integration，不能只凭单目录测试通过就判断 CI 可收集。根 dev extras 同时声明插件
单元测试需要的 regex；插件本身仍通过独立分发元数据声明运行时依赖。

真实框架验收还须安装固定 revision 的 agent-core，并先显式确认依赖可导入：

```bash
python -c "from openjiuwen.core.runner import Runner; from openjiuwen.core.single_agent.rail.base import AgentRail; from jiuwen_memory.api import assemble_server_runtime"
python -m pytest tests/unit/jiuwen_memory_adapter/agent_security tests/integration/jiuwen_memory_adapter/agent_security --confcutdir=tests/integration/jiuwen_memory_adapter/agent_security -o addopts= --tb=short --show-capture=no -q
```

测试使用真实 SDK/框架和本地 HTTP 协议服务，覆盖三种选择、最终请求、重试、来源撤权、
多 agent 双向消息和结果释放。框架缺失会触发 `importorskip`，跳过不算验收通过。
仓库自带工作流默认运行 unit；平台门禁另运行整个 tests。在尚未具备 #406 的公开
隐私 SDK 时，相关 SDK fixture 明确报告依赖缺失并跳过；能力存在后的装配或安全
失败仍须失败。框架缺失或依赖未合入导致的跳过，都不能作为真实闭环验收通过。

浏览器验收另需 `uvicorn`、Node、Playwright 和兼容浏览器，路径由环境显式指定：

```powershell
python -m pip install uvicorn
# 使用已安装的 playwright-core；下面路径均替换为本机实际路径。
$env:MINDVAULT_PLAYWRIGHT = "C:/path/to/playwright-core/index.mjs"
$env:MINDVAULT_BROWSER = "C:/path/to/msedge.exe"
python -m pytest tests/integration/jiuwen_memory_adapter/agent_security/test_browser_conversation.py --confcutdir=tests/integration/jiuwen_memory_adapter/agent_security -o addopts= -q
```

独立构建：

```bash
python -m pip wheel --no-deps ./jiuwen_memory_adapter/agent_security --wheel-dir /path/to/wheel-output
```

当前元数据未设置 README 为 package long description；文档/示例应从源码入口阅读，
不能假设它们已装进 wheel。后续完善分发元数据时再核对发布清单。

稳定契约见 [S10](../../docs/specs/S10-security.md#agent-侧敏感记忆使用与外发契约)，
设计取舍见 [F04](../../docs/features/common/F04-security-interfaces-and-encryption.md#11-agent-侧敏感记忆使用与外发管控)，
模块维护见 [AGENTS.md](AGENTS.md)。
