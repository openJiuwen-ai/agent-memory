# S08 — 安全横切契约（Security）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `src/common/authentication/`、`src/common/credential_store/`、`src/common/admission/`、`src/common/encryption/`、`bootstrap/`、`src/api/` |
| 最近一次修订日期 | 2026-08-05 |
| 关联特性文档 | `docs/features/security/F01-authentication-kernel.md`，`docs/features/common/F04-security-interfaces-and-encryption.md` |

## 范围 / 边界

本规约定义请求认证、凭据存储、准入控制、静态加密配置和服务绑定的不变量。
授权角色与身份一致性将在角色授权特性落地时由 S03 扩展；审计完整性由对应审计特性扩展。

## 不变量

1. 请求身份只由认证中间件产生，客户端 payload 中的身份字段不是可信身份来源。
2. `Authenticator.authenticate` 成功返回 `AuthContext`，失败抛 `AuthenticationError`；不得返回默认身份。
3. `Authenticator.requires_loopback_binding()` 默认返回 `True`。只有实现显式声明可远程暴露，surface 才能绑定非 loopback 地址。
4. 公开 `HttpServer.serve(host, port)` 必须在真正 bind 前执行 loopback guard，不能只依赖 CLI `main()`。
5. `authenticator` 或 `rate_limiter` 存在多个实例时必须定义 `default`，否则拒绝启动。
6. `requires_concurrency_guard()` 默认返回 `True`；轻量实现必须显式返回 `False` 才能跳过进程级 guard。
7. `Scope` 与 `AuthContext` 是 frozen value object，认证后不得原地改写身份或隔离维度。
8. YAML 只选择已注册 target 并传递 params，不接受 Python import path 或任意类加载。

## 注册与配置

每个能力目录的 `base.py` 定义抽象接口和 Producer，`*_impl/` 中的实现通过
`@Producer.register("target")` 注册，`common.bootstrap.register_plugins()` 在配置解析前统一触发。

实现模块必须在配置解析前由 `common.bootstrap.register_plugins()` 或应用自己的注册入口
import，注册装饰器才会生效。当前核心不自动发现任意外部 Python 包；外部插件应由宿主应用
在 `Server.build` / `build_kernel` 前显式加载。

```yaml
authenticator:
  default:
    target: api_key
    params:
      key_store: default
      root_api_key: ${ROOT_API_KEY}
key_store:
  default:
    target: memory
rate_limiter:
  default:
    target: token_bucket
    params:
      capacity: 30
      refill_per_sec: 10
encryption:
  default:
    target: local
    params:
      key_env: AGENT_MEMORY_MASTER_KEY
```

## 当前扩展边界

- Authenticator、PrincipalKeyStore、RateLimiter、EncryptionProvider 均可通过 Producer 注册扩展。
- Server 按 capability 决策绑定和并发保护，不按封闭枚举分支。
- 认证根装配消费一个最终实例；需要多认证串联时，应注册组合 target，由该 target 通过
  `Producer.dep()` 引用多个具名实例，而不是让 YAML 隐式并行执行。
- EncryptedKVStore/EncryptedFSStore 只负责存储边界接线，密码学实现归 `common/encryption`。
- `FsProducer` 已能独立装配 FSStore，但当前 `build_kernel` 主业务链路没有 FSStore 消费点；
  仅写 YAML 不会自动让记忆资产经过 FS 加密，接入前须先定义资产写入/读取消费者和 API 契约。
