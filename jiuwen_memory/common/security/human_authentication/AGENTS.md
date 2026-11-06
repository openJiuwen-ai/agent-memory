# 独立人机认证

**规约文档**：[S10 安全横切契约](../../../../docs/specs/S10-security.md)；
配置见 [S08](../../../../docs/specs/S08-config.md#隐私配置)。

## 模块地图

- `base.py`：HumanAuthenticator 与 HumanAuthenticatorProducer，独立于机机 Authenticator。
- `human_authentication_impl/passkey.py`：WebAuthn 用户验证、一次性登记邀请和挑战/公钥状态。

## 行为铁律

- 登记邀请由可信部署方通过独立渠道发放，摘要绑定固定 USER 主体；机机 API Key 不能登记人。
- 挑战绑定浏览器和具体操作，过期、失败及成功都不能重放；先原子消费挑战，再验证证明。
- WebAuthn 同时验证 RP ID、精确 origin、签名、用户验证与计数器；不降级为普通 Bearer 认证。
- 登记要求可发现 Passkey；认证选项不枚举所有已注册凭据。公钥登记与计数器检查事务化。
- 可选依赖在装配选中 passkey 时加载；不依赖服务端 Windows Hello 或交互桌面。

## 与其他子目录的边界

只证明注册的人类主体；记忆访问权限仍由 API/Authorizer 判定。HTTP 传输负责精确来源、
HttpOnly/SameSite cookie、有界 JSON、CSP 和 no-store；普通 MemoryAPI 不接受人机证明。
