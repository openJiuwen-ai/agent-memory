# F02 — DashScope LLM Provider Adapter

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-12 |
| 影响范围 | jiuwen_memory/common/llm，deploy/docker，docs/specs/S07-common.md |
| 测试基线 | Provider 定向单测 25 passed；`pytest -m unit` 99 passed；全量 472 passed / 55 skipped / 2 failed（当前环境未安装可选依赖 `torch`） |
| Refs | — |

## 背景

项目原先只有通用 `openai` target。阿里云 DashScope 虽提供 OpenAI-compatible
Chat Completions，但思考开关使用非标准的 `extra_body.enable_thinking`。若在
Extractor、Evolver 或 Judge 等业务调用点直接注入该字段，会把阿里云协议
细节扩散到多个模块，也可能使其他 OpenAI-compatible 后端拒绝未知字段。

## 决策

1. 保留通用 `OpenAILLM`，不默认注入任何厂商扩展字段。
2. 新增 `DashScopeLLM`，以 `target: dashscope` 注册，复用 OpenAI-compatible
   传输并在 Adapter 内生成 `extra_body.enable_thinking`。
3. `DashScopeLLM` 缺省 `enable_thinking=false`；显式 `true` 开启，显式
   `null` 则不发送该字段。环境变量展开后的字符串在装配阶段严格解析。
4. Docker 真实部署的 `llm.default` 切换为 `dashscope`；内置无配置离线栈继续
   使用 `echo`，保持无凭证、无网络的确定性测试能力。

## 拒绝的方案

### 在所有 LLM 调用点硬编码 `extra_body`

厂商协议侵入构建、检索和评测层，切换 Provider 时需要多点修改，且容易漏改。

### 在通用 `OpenAILLM` 中默认关闭思考

`enable_thinking` 不是 OpenAI 标准字段。所有 OpenAI-compatible 后端都收到该值
会降低兼容性，也会错误表达一个并不存在的跨平台统一协议。

### 根据 URL 或模型名自动推断 Provider

代理地址、私有部署和自定义模型名都会导致误判，因此 Provider 必须由
`target` 显式选择。

## 验证

- DashScope 默认、显式开启、显式不发送三种状态。
- `chat()` 与 `health()` 使用相同的 Provider 选项。
- 通用 OpenAI Provider 不出现 DashScope 字段。
- Docker online/local 配置均默认 `target: dashscope` 且关闭思考。

## 已知遗留

- 未将 Provider-specific `params` 升级为独立的强类型配置类；当 Provider 数量增多后
  再评估统一的配置 schema。
- evaluation 的 answer/judge LLM 依照当前评测独立配置保持不变，本特性不将其
  改造为内核 Provider Adapter。
- 仅可切换思考的模型能保证 `enable_thinking=false` 生效；思考专用模型的
  服务端限制不能由客户端 Adapter 规避。
