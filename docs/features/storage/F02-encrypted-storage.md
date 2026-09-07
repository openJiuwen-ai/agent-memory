# F02 — 加密 KV 存储设计（EncryptedKVStore）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27；2026-09-07 按 PR1 范围收敛 |
| 影响范围 | `jiuwen_memory/storage/kv_impl/encrypted_kv_store.py`、`jiuwen_memory/common/security/cryptography/`、`docs/specs/S06-storage.md`、`docs/specs/S10-security.md` |
| 测试基线 | `tests/unit/storage/test_encrypted_kv_store.py` 覆盖加密写入、解密读取、批量读、scan/list、透传操作、工厂装配与失败关闭 |
| Refs | — |

> FS 加密装饰器不属于本次 PR1。其实现已独立保存在 `sec/fs-encryption`，待资产写入链路、
> 文件大小边界和 API 契约明确后单独评审合入。

## 背景

KVStore 是 `MemoryUnit` 内容、原始消息与部分控制数据的真源字节存储。若把加解密分散到
`add`、`search`、`get` 等上层入口，每条路径都会重复处理密钥、AAD 与错误语义，新入口也
容易漏接。因此加密收敛在 KV 边界：调用方仍使用标准 `KVStore`，raw 后端只接触密文。

## 决策

1. **`EncryptedKVStore` 是装饰器，不是物理后端。** 它包装显式配置的
   `raw_kv_store`；raw 后端可以是 memory、sqlite、redis 或其他 KV 实现。
2. **只加密 value。** `insert` / `update` 写入前加密，`get` / `mget` / `scan` 读取后
   解密；`list` 必须先解密，再做 MemoryUnit 过滤、计数、排序和分页。key 与 scope 保持
   可寻址，`exists` / `delete` / `scopes` 直接透传。
3. **算法和密钥管理不进 storage。** storage 只构造 `CryptoContext` 与稳定 AAD，并调用
   `CryptographyProvider`。provider 只能经独立 `KeyProvider` 获取密钥。
4. **AAD 绑定完整对象语义。** AAD 绑定
   `Scope(org/space/user/agent/session)`、KV key、用途和格式版本。用途由 key 前缀确定：
   `/memory/` 为 `memory_unit`，`/messages/` 为 `raw_message`，其他为 `kv_value`。
5. **严格失败关闭。** 非法信封、AAD 不匹配、未知密钥或任何解密异常统一转为
   `BackendError`；不返回密文、不跳过坏记录、不回退明文。是否加密由部署选择 raw 或
   encrypted Store 表达，同一个装饰器没有 `allow_plaintext` 开关。
6. **具名依赖共享。** `raw_kv_store` 与 `cryptography` 都使用 Producer 具名引用。
   `SecurityRuntime` 若引用相同的 `cryptography` 名称，必须与存储取得同一对象。
7. **装配失败要早。** 缺少 `raw_kv_store`、引用自身或缺少密码学依赖时，在构建期拒绝；
   不能静默创建临时内存后端或透传 provider。

## 配置

```yaml
cryptography:
  default:
    target: local
    params:
      key_provider: default

key_provider:
  default:
    target: local
    params:
      key_file: ~/.agent-memory/security/master.key

kv_store:
  raw:
    target: sqlite
    params:
      db_path: agent_memory.db
  default:
    target: encrypted
    params:
      raw_kv_store: raw
      cryptography: default
```

上一版 `security.<name>.target=local` 与 `params.security` 会在配置解析期自动迁移一个发布
周期并发出 `DeprecationWarning`。旧 `allow_plaintext` 不会恢复明文回退。

## 拒绝的方案

- **在 MemoryAPI 或各 engine 动词中分别加密**：入口多且容易遗漏，无法形成单一存储边界。
- **每个 raw KV 后端各自实现加密**：会重复 AAD、错误收敛和密钥接线。
- **storage 直接读取根密钥**：破坏 `KeyProvider` 的托管与轮换边界。
- **加密 key / scope**：会破坏 scan、exists、delete、TTL、空间清理和审计定位；隐藏元数据
  需要单独设计 opaque key 或加密检索。
- **解密失败返回原值或跳过记录**：会把安全错误伪装成正常业务数据。

## 验证

- `insert` / `update` 写入 raw KV 的 value 不含明文，`get` / `mget` 返回原始明文。
- `scan` 对每个 key 使用各自 AAD；跨 scope、跨 key 搬运密文不能解开。
- `list` 在解密后执行过滤和分页；任一坏记录使整个调用失败。
- `exists` / `delete` / `scopes` 不触发解密。
- 工厂能按具名 `raw_kv_store` / `cryptography` 装配并共享实例；缺依赖或自引用拒绝。
- 非 ENC1、篡改密文、错误 AAD 与 provider 异常均 fail-closed 为 `BackendError`。

## 已知遗留

- 默认配置仍使用 raw memory KV；部署必须显式选择 `target: encrypted`。
- 只保护 KV value。vector、fulltext、fusion、graph 中的文本、向量与图属性不在本装饰器
  范围内；向量保护需要加密检索设计。
- key、scope、TTL 与记录数量仍对 raw 后端可见。
- `LocalKeyProvider` 的轮换历史只保存在进程内；跨重启轮换、KMS/Vault 与批量重加密流程
  尚未实现。
- CloudEngine 开启 encrypted KV 后的端到端静态加密集成测试仍需补充。
