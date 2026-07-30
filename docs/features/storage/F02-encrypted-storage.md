# F02 — 加密 KV 存储设计（EncryptedKVStore）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27 |
| 影响范围 | src/storage/kv_impl/encrypted_kv_store.py，src/storage/kv_impl/__init__.py，src/common/security/，docs/specs/S06-storage.md，docs/features/common/F04-security-interfaces-and-encryption.md |
| 测试基线 | `tests/unit/storage/test_encrypted_kv_store.py` 覆盖加密写入、读后解密、scan 解密、透传操作、工厂装配与失败关闭；`pytest -q tests/unit/storage`、相关模块测试、ruff 与 `git diff --check` 已通过 |
| Refs | — |

## 背景

KVStore 是 `MemoryUnit` 内容、原始消息与部分控制数据的真源字节存储。未加密时，落盘后端或远端 KV 后端可以直接看到 value 明文；但如果把加解密逻辑分散到 `write`、`recall`、`get` 等上层接口，会导致每条读写路径都要重复处理开关、密钥、AAD 与错误语义，也容易让新增入口绕过加密。

因此加密能力需要落在 KV 边界：对调用方保持 `KVStore` 合同不变，对底层后端只写入密文。算法、密钥来源、明文兼容策略不归 storage 层管理，而是由 `src/common/security/` 的 `SecurityProvider` 提供。

## 决策

1. **EncryptedKVStore 是 KV 装饰器，不是真正的物理后端**

   `EncryptedKVStore` 仍实现 `KVStore` 接口，但内部必须包装一个 `raw_kv_store`。上层 `MemoryAPI`、engine、construction、retrieval 只看见标准 KVStore；底层 raw store 可以是 `memory`、`sqlite`、`redis` 或后续新增的 KV 实现。

2. **加密边界限定为 KV value**

   `insert` / `update` 在写入 raw KV 前加密 `bytes` value；`get` / `scan` 从 raw KV
   读出后解密再返回。`list` 扫描并解密 `/memory/` 条目后，再执行公共 MemoryUnit
   过滤、计数、排序和分页，不能把明文过滤条件委托给 raw KV。

3. **算法与密钥管理委托给 SecurityProvider**

   storage 层只构造 `SecurityContext` 与 AAD，然后调用 `SecurityProvider.encrypt/decrypt`。AES-GCM、本地密钥文件、KMS、Vault、轮换策略、明文兼容策略都属于 `common/security` 或具体 provider 的职责。

4. **AAD 绑定 scope、key 与用途**

   AAD 使用稳定的 JSON 字节序列，版本号为 `1`，并绑定 `Scope(org/space/user/agent/session)`、逻辑 `key` 与 `purpose`。同一段密文如果被移动到其他 space、其他 key 或其他用途下，解密应失败。

   `purpose` 由 key 前缀推导：

   | key 前缀 | purpose |
   |---|---|
   | `/memory/` | `memory_unit` |
   | `/messages/` | `raw_message` |
   | 其他 | `kv_value` |

5. **失败关闭**

   加密或解密异常统一转成 `BackendError`。`scan` 中任意一条记录无法解密时，整个 scan 调用失败；storage 层不返回密文、不静默跳过坏数据，也不自行回退明文。是否允许旧明文数据兼容读取，由 provider 的配置决定。

6. **配置通过嵌套 KV 实例完成**

   推荐把 raw KV 作为内部实例声明，再把默认 KV 指向 `encrypted`：

   ```yaml
   security:
     default:
       target: local
       params:
         key_file: ~/.agent-memory/security/master.key
         allow_plaintext: false

   kv_store:
     raw:
       target: sqlite
       params:
         db_path: agent_memory.db

     default:
       target: encrypted
       params:
         raw_kv_store: raw
         security: default
   ```

   其他模块继续依赖 `kv_store.default` 时，读写路径自然经过加密包装；`kv_store.raw` 只作为加密装饰器的内部依赖，不应暴露给业务读写入口。

7. **与 space 多租户隔离叠加**

   scope 仍由 raw KV 的命名空间或字段隔离实现；`space` 是 scope 的硬分区维度。EncryptedKVStore 在 AAD 中再次绑定完整 scope，因此即使底层密文被错误复制到另一个 space，也不能作为合法明文被读取。

8. **删除与生命周期操作不解密**

   `exists`、`delete`、`scopes` 只依赖 key/scope，不需要读取 value，因此直接透传给 raw KV。租户删除、session 清理、TTL 过期等生命周期操作删除的是密文记录，不要求先解密。

## 拒绝的方案

- **在 MemoryAPI/write/recall/get 中分别调用 security**：被拒。上层入口太多，且未来新增 engine 或批处理入口时容易遗漏；KV 装饰器可以把加密收敛到单一边界。
- **每个 raw KV 后端各自实现加密**：被拒。memory/sqlite/redis 会重复实现 AAD、失败关闭与明文兼容策略，后续增加后端时也会复制安全逻辑。
- **storage 层直接实现加密算法和密钥管理**：被拒。storage 只负责存取语义，不应持有算法选择、密钥加载、KMS/Vault 访问、轮换策略等安全治理能力。
- **同时加密 key 和 scope 命名空间**：本阶段拒绝。完全隐藏 key/scope 会破坏 scan、exists、delete、TTL、space 清理与审计定位。后续如需隐藏元数据，应单独设计 opaque key 或索引加密方案。
- **解密失败时返回密文或跳过记录**：被拒。这会把安全错误伪装成业务数据，导致调用方在不知情的情况下继续处理损坏或越界数据。

## 验证

既有单测应覆盖以下行为：

- `insert` / `update` 写入 raw KV 的 value 不是明文，`get` 返回原始明文。
- `scan` 对每个 key 单独构造 AAD 并返回解密后的 `(key, value)`。
- `exists`、`delete`、`scopes` 透传给 raw KV，不触发解密。
- factory 可以通过 `raw_kv_store` 与 `security` 依赖装配出 encrypted KV。
- `raw_kv_store` 缺失或指向自身时构造失败，避免递归装配。
- 解密失败统一抛 `BackendError`，不返回密文或部分结果。
- provider 开启明文兼容时可以读取历史明文数据；关闭时保持严格失败关闭。

当前文档变更的轻量校验基线：

```bash
git diff --check
```

## 已知遗留

- 默认配置不会自动切到 encrypted KV，调用方必须显式把业务使用的 `kv_store` 实例指向 `target: encrypted`。
- 当前只保护 KV value；vector/fulltext/fusion/graph/fs 中的索引字段、文本、向量、图边、文件资产不在该装饰器保护范围内。
- key、scope 维度、TTL 与 raw 后端中的记录数量仍对后端可见。
- KMS/Vault provider、密钥轮换、密钥版本迁移、批量重加密仍需在 `common/security` 与运维流程中补齐。
- cloud engine 与 encrypted KV 的端到端集成测试、space 删除后的密文清理验证、严格关闭明文兼容后的迁移验证仍需补充。
