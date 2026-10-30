# 安全功能示例

使用步骤、保护边界和配置说明见 [安全使用指南](../../docs/zh/API文档/security.md)。
从仓库根目录设置指南第 3 节的 Python 环境后执行，无需复制脚本到仓库根目录。

| 命令 | 验证内容 |
|---|---|
| `python examples/security/quickstart.py` | DEV USER 写入、读取和检索 |
| `python examples/security/api_key.py` | DEV/API Key 切换、Root 检查、同服务签发 USER Key、HTTP 调用 |
| `python examples/security/key_revocation.py` | Key 撤销后旧请求上下文拒绝 |
| `python examples/security/grant_http.py` | HTTP 分享前拒绝、分享后可读、撤销后拒绝 |
| `python examples/security/grant_sdk.py` | SDK 的同一 Grant 流程 |
| `python examples/security/space.py` | 空间创建、成员、内容访问 |
| `python examples/security/delegation.py` | 有限委托、agent 身份、作者归属与撤销 |
| `python examples/security/gateway.py --self-test` | 纯 Trusted 配置、公开主体初始化、网关协议与业务授权 |
| `python examples/security/encryption.py` | 统一开关下应用 API 写入、读取和检索 |
| `python examples/security/audit.py` | 临时审计链和实际 HTTP/CLI 验证 |
| `python examples/security/mcp_stdio.py` | 安装 MCP extra 后的真实 stdio 会话与 Root 引导 |

默认演练使用进程内数据、回环临时端口和系统临时审计文件，不需要模型、数据库或生产凭据。
脚本中的随机密钥只供该进程及其子进程使用，不打印、不交付为正式用户 Key。
`gateway.py` 不加 `--self-test` 时保持 8137/8139 监听，需要 `DEMO_CLIENT_KEY`；Ctrl+C 关闭。
`grant_http.py --server URL` 会写入指定测试服务，仅用于指南配置的隔离测试实例。

示例通过日志输出 `PASS` 结果，验收失败会抛出异常并以非零状态退出。
CI 在 Python 3.11/3.12 上以普通和优化模式运行 `tests/integration/security/test_usage_examples.py`，
确保启用 `python -O` 后仍执行全部验收步骤。
这些本地检查不等于完成了 Linux LUKS、Docker、ES 或其他真实后端的加密部署验收。
