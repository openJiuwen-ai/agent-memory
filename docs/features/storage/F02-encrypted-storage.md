# F02 — 加密存储设计（EncryptedKVStore / EncryptedFSStore）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27（KV 侧）；2026-07-29（FS 侧补入） |
| 影响范围 | src/storage/kv_impl/encrypted_kv_store.py，src/storage/kv_impl/__init__.py，src/storage/fs_impl/encrypted_fs_store.py，src/storage/fs_impl/__init__.py，src/common/security/cryptography/，docs/specs/S06-storage.md，docs/features/common/F04-security-interfaces-and-encryption.md |
| 测试基线 | KV 侧：`tests/unit/storage/test_encrypted_kv_store.py` 覆盖加密写入、读后解密、scan 解密、透传操作、工厂装配与失败关闭。FS 侧：`tests/unit/storage/test_encrypted_fs_store.py` 13 条全绿；单元全量 `15 failed, 814 passed, 1 skipped`，15 个失败全为预存在的环境失败（14 个 `test_jieba_tokenizer.py` 缺 `nlp` extra，1 个上游 `test_local_envelope.py` 断言 `0o600` 权限位、Windows `os.chmod` 设不出来，已在纯上游代码上复现）。相关模块测试、ruff 与 `git diff --check` 已通过 |
| Refs | — |

> **2026-08-05 F05 迁移后记。** 本文记录的是 2026-07 的决策过程，正文保留原貌。
> 三处已被 F05 Common Security 推翻或改名，以下述为准：
>
> 1. **`EncryptionProvider` → `CryptographyProvider`**，落点从 `src/common/encryption/`
>    迁到 `src/common/security/cryptography/`；`EncryptionContext` → `CryptoContext`，
>    对象标识与格式版本提为专有字段 `object_id` / `format_version`，不再塞 `metadata`。
>    配置顶层段名与装配参数名均由 `encryption` 改为 `cryptography`。
> 2. **决策 11 与 `allow_plaintext` 已作废**（F05 §明文策略）。不再有任何明文回退开关：
>    不是合法信封就拒绝读取，解密失败绝不返回原始 bytes。是否允许未加密存储，由上层
>    选 `encrypted` 还是 raw target 表达。相应的明文兼容测试已删除。
> 3. **「无根密钥轮换接缝」已部分补上**：信封升级到 v2，头部自述 key id 与 key epoch，
>    根密钥改由独立的 `KeyProvider` 提供（`TOP_NAME` 为 `key_provider`），换 KMS/Vault
>    不必改加密实现。写出一律 v2、v1 只读兼容；跨代 keyring 轮换仍未实现。
>
> 决策 1–10、12 与其余「拒绝的方案」不受影响。

## 背景

KVStore 是 `MemoryUnit` 内容、原始消息与部分控制数据的真源字节存储。未加密时，落盘后端或远端 KV 后端可以直接看到 value 明文；但如果把加解密逻辑分散到 `write`、`recall`、`get` 等上层接口，会导致每条读写路径都要重复处理开关、密钥、AAD 与错误语义，也容易让新增入口绕过加密。

因此加密能力需要落在 KV 边界：对调用方保持 `KVStore` 合同不变，对底层后端只写入密文。算法、密钥来源、明文兼容策略不归 storage 层管理，而是由 `src/common/encryption/` 的 `EncryptionProvider` 提供。

**FS 侧同理，且更迫切**：原模态资产（上传的文档、图片、音视频）走 `FSStore`，它们往往比 KV 里的结构化记忆更敏感，却完全裸着落盘。静态加密保护的是**访问路径之外**的泄露面——拿到磁盘快照的人绕过了认证与授权，因为快照根本不走访问路径。KV 侧先落地，FS 侧随后按同一形态补齐。

## 决策

1. **EncryptedKVStore 是 KV 装饰器，不是真正的物理后端**

   `EncryptedKVStore` 仍实现 `KVStore` 接口，但内部必须包装一个 `raw_kv_store`。上层 `MemoryAPI`、engine、construction、retrieval 只看见标准 KVStore；底层 raw store 可以是 `memory`、`sqlite`、`redis` 或后续新增的 KV 实现。

2. **加密边界限定为 KV value**

   `insert` / `update` 在写入 raw KV 前加密 `bytes` value；`get` / `scan` 从 raw KV
   读出后解密再返回。`list` 扫描并解密 `/memory/` 条目后，再执行公共 MemoryUnit
   过滤、计数、排序和分页，不能把明文过滤条件委托给 raw KV。

3. **算法与密钥管理委托给 EncryptionProvider**

   storage 层只构造 `EncryptionContext` 与 AAD，然后调用 `EncryptionProvider.encrypt/decrypt`。AES-GCM、本地密钥文件、KMS、Vault、轮换策略、明文兼容策略都属于 `common/encryption` 或具体 provider 的职责。

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

   其他模块继续依赖 `kv_store.default` 时，读写路径自然经过加密包装；`kv_store.raw` 只作为加密装饰器的内部依赖，不应暴露给业务读写入口。

7. **与 space 多租户隔离叠加**

   scope 仍由 raw KV 的命名空间或字段隔离实现；`space` 是 scope 的硬分区维度。EncryptedKVStore 在 AAD 中再次绑定完整 scope，因此即使底层密文被错误复制到另一个 space，也不能作为合法明文被读取。

8. **删除与生命周期操作不解密**

   `exists`、`delete`、`scopes` 只依赖 key/scope，不需要读取 value，因此直接透传给 raw KV。租户删除、session 清理、TTL 过期等生命周期操作删除的是密文记录，不要求先解密。

## FS 侧：EncryptedFSStore

### 9. FS 装饰器与 KV 装饰器同构，不自带任何密码学

`EncryptedFSStore` 做的事和 `EncryptedKVStore` 逐条对应：构造 `EncryptionContext` 与 AAD，转发给注入的 `EncryptionProvider`。密码学一行都不在 storage 里。

依赖方向 `storage → common.encryption`，单向；encryption 不认识 Store。回归防线：`test_encrypted_fs_is_registered_by_storage_bootstrap`。

### 10. 装饰器住在 `src/storage/`，不住在 `src/common/encryption/`

理由不是分层美学，是**消费边界**：存储装饰器随 storage bootstrap 注册，密码学实现由
`common.bootstrap.register_plugins()` 注册。直接调用 `build_kernel` 与经 `Server.build` 的入口
使用同一套已注册 target，不会出现只在某种入口缺实现的故障。

这与 KV 侧的落点一致，FS 侧只是照做。

### 11. 明文兼容开关只在 provider 上，装饰器不重复提供

「读到非 ENC1 的数据怎么办」有两个对立的正确答案，各自对应一个部署阶段：

- **迁移期必须宽松**——加密层上线时库里全是加密前的明文，一律拒绝就是上线即全量不可读。
- **迁移完成后必须收紧**——此时「读到明文」只可能是有人绕过加密层直接写了底层存储。宽松模式会静默放行，而这正是降级攻击的着力点。

`LocalEnvelopeEncryptionProvider` 的 `allow_plaintext` 参数已经管这件事（决策 5 的最后一句）。两个装饰器都不再重复提供同语义旋钮：两个开关意味着两处配置、两种组合，其中「装饰器宽松 + provider 严格」这类组合没有任何意义，只会在排查时多一个要查的地方。

**写路径永远加密**，与开关无关——`test_encrypted_fs_store_write_always_encrypts_even_when_plaintext_allowed` 钉住这条。开关若顺带放松了写，迁移期写进去的数据会永远是明文而调用方毫无察觉。

### 12. FS 加密整个文件内容，`ref` 与 scope 保持明文

与决策 2（KV 只加密 value）同理：路径要能寻址，加密 `ref` 就没法 `get`/`stat`/`delete`。泄露的信息是「有哪些文件」，不是文件里是什么。

### 13. AAD 绑满五维 scope + `ref`

与决策 4 同构，`key` 换成 `ref`，`purpose` 固定为 `fs_object`。

不绑 AAD 时，只要根密钥相同（同一部署），把 org A 的密文块搬进 org B 的存储位置就能解开——加密在这种攻击下等于没有。只绑 `org` 会让同 org 内的用户互读。存储层的 scope 隔离是**访问控制**，可以被绕过（直接写底层、备份恢复串了）；AAD 是密码学的，绕不过。

`space` 是 `Scope` 五维化时新加的维度，漏了它同 org 下的两个 space 就能互读。回归防线：`test_encrypted_fs_store_aad_binds_all_five_scope_dimensions`、`test_encrypted_fs_store_cross_scope_ciphertext_move_fails`。

### 14. 解密失败一律 `BackendError`，不透传底层异常

与决策 5 一致。provider 抛的 `KeyMismatchError` / `AuthenticationFailedError` 对运维有诊断价值，但它们不是跨层契约——装饰器把它们收敛成 `BackendError` 并在消息里带上 `ref`，原异常经 `raise ... from exc` 保留在 `__cause__` 里，traceback 上一行不丢。

### 15. `inner` 无默认值

给 `inner` 一个默认会让「配错了」静默变成「加密了一个空的内存 store」——数据写得进去，重启后全没了。未配置时在装配期抛 `ValidationError`，并拒绝自引用。回归防线：`test_encrypted_fs_store_factory_requires_inner_dependency`。

（KV 侧的 `raw_kv_store` 同此约束；FS 侧的参数名是 `inner`，与 `fs_store` 既有的装饰器命名一致。）

### 16. 默认关闭

与决策 6 一致：不配 `target: encrypted` 就没有任何加密行为。现有部署零影响，不需要迁移。

FS 侧配置形态：

```yaml
cryptography:
  main_sec:
    target: local
    params:
      key_provider: main_key

key_provider:
  main_key:
    target: local
    params:
      key_file: /etc/agent-memory/master.key

fs_store:
  raw_fs:
    target: local
    params: { root: /var/lib/agent-memory/files }
  main_fs:                        # 上层引用这个
    target: encrypted
    params:
      inner: raw_fs
      cryptography: main_sec
```

## 拒绝的方案

- **在 MemoryAPI/write/recall/get 中分别调用 encryption**：被拒。上层入口太多，且未来新增 engine 或批处理入口时容易遗漏；KV 装饰器可以把加密收敛到单一边界。
- **每个 raw KV 后端各自实现加密**：被拒。memory/sqlite/redis 会重复实现 AAD、失败关闭与明文兼容策略，后续增加后端时也会复制安全逻辑。
- **storage 层直接实现加密算法和密钥管理**：被拒。storage 只负责存取语义，不应持有算法选择、密钥加载、KMS/Vault 访问、轮换策略等安全治理能力。
- **同时加密 key 和 scope 命名空间**：本阶段拒绝。完全隐藏 key/scope 会破坏 scan、exists、delete、TTL、space 清理与审计定位。后续如需隐藏元数据，应单独设计 opaque key 或索引加密方案。
- **解密失败时返回密文或跳过记录**：被拒。这会把安全错误伪装成业务数据，导致调用方在不知情的情况下继续处理损坏或越界数据。
- **chunked encryption（FS 侧分块加密以支持流式读）**：本阶段拒绝。F04 §5.3 自己就说了它不适合作默认方案——chunk 之间没有密码学绑定，可以被重排、截断、拼接。代价是 `FSStore.get` 必须读全文件到内存才能解密，大文件会吃内存，见「已知遗留」。
- **在装饰器上再开一个 `allow_plaintext_read`**：被拒。见决策 11，两个同语义开关只会制造无意义的组合与多余的排查点。

## 验证

既有单测应覆盖以下行为：

- `insert` / `update` 写入 raw KV 的 value 不是明文，`get` 返回原始明文。
- `scan` 对每个 key 单独构造 AAD 并返回解密后的 `(key, value)`。
- `exists`、`delete`、`scopes` 透传给 raw KV，不触发解密。
- factory 可以通过 `raw_kv_store` 与 `encryption` 依赖装配出 encrypted KV。
- `raw_kv_store` 缺失或指向自身时构造失败，避免递归装配。
- 解密失败统一抛 `BackendError`，不返回密文或部分结果。
- provider 开启明文兼容时可以读取历史明文数据；关闭时保持严格失败关闭。

当前文档变更的轻量校验基线：

```bash
git diff --check
```

### FS 侧断言（`tests/unit/storage/test_encrypted_fs_store.py`，13 条全绿）

| 断言 | 落点 |
|---|---|
| 内层存的是密文，且不含明文片段 | `test_encrypted_fs_store_encrypts_content_and_decrypts_get` |
| 交给 provider 的 `EncryptionContext` 带对 scope / purpose / ref | 同上 |
| AAD 绑满五维 scope + ref | `test_encrypted_fs_store_aad_binds_all_five_scope_dimensions` |
| 换 scope 搬密文解不开（绕过访问控制后仍拦得住） | `test_encrypted_fs_store_cross_scope_ciphertext_move_fails` |
| `update` 也加密（第二条写路径） | `test_encrypted_fs_store_update_also_encrypts` |
| 空文件 roundtrip | `test_encrypted_fs_store_roundtrips_empty_file` |
| `stat.size` 是密文长度（已知代价，显式钉住） | `test_encrypted_fs_store_stat_reports_ciphertext_size` |
| `get`/`delete` 的 NotFound 与幂等语义不被加密改变 | `test_encrypted_fs_store_passes_through_missing_and_delete` |
| ~~迁移期明文可读（provider 允许时）~~ | ~~`test_encrypted_fs_store_supports_plaintext_compatibility_via_provider`~~（F05 §明文策略作废，用例已删） |
| ~~明文兼容开着时写路径**仍然**加密~~ | ~~`test_encrypted_fs_store_write_always_encrypts_even_when_plaintext_allowed`~~（同上） |
| 解密失败 fail-closed 成 `BackendError` | `test_encrypted_fs_store_decryption_failure_is_fail_closed` |
| 具名依赖装配 / 缺 `inner` 报错 | `test_encrypted_fs_store_factory_*` |
| `encrypted` 在只调 `register_backends()` 时已注册 | `test_encrypted_fs_is_registered_by_storage_bootstrap` |

## 已知遗留

- 默认配置不会自动切到 encrypted KV，调用方必须显式把业务使用的 `kv_store` 实例指向 `target: encrypted`。FS 侧同理（`fs_store` 的 `target: encrypted`）。
- 当前只保护 KV value 与 FS 文件内容；vector/fulltext/fusion/graph 中的索引字段、文本、向量、图边不在装饰器保护范围内。**向量本身可被反演出近似原文**，这是一个真实的信息泄露面，但加密向量就没法做 ANN 检索——需要的是加密检索方案，不是装饰器能解决的。
- key、ref、scope 维度、TTL 与 raw 后端中的记录数量仍对后端可见。
- **`FSStore.get` 必须读全文件到内存**才能解密（见「拒绝的方案」里的 chunked encryption）。大文件（视频、模型权重）会吃内存。
- **`FileStat.size` 返回密文长度**，比明文长（信封头 + 包装后的数据密钥 + 两个 nonce + 两个 16B GCM tag）。不修正——修正需要先解密才能知道明文长度，代价荒谬。调用方拿它分配缓冲区只会偏大，不影响正确性。
- ~~**无根密钥轮换接缝**~~：已在 F05 迁移中补上。信封升级到 v2，头部自述 key id 与
  key epoch，根密钥由独立的 `KeyProvider` 提供。**跨代轮换仍未实现**——keyring 保留旧
  epoch、按信封自述的 key ref 选密钥这一步还没有，换根密钥依旧要重加密历史密文。
- **`LocalKeyProvider` 的根密钥是磁盘上的明文文件**。生产应走 KMS/Vault。
- **根密钥文件权限在 Windows 上设不出 `0o600`**，对应断言已加 `os.name != "nt"` 守卫，
  只在 POSIX 上校验。不影响 Linux 部署。
- KMS/Vault KeyProvider、跨代密钥轮换、密钥版本迁移、批量重加密仍需在
  `common/security/cryptography/` 与运维流程中补齐。
- cloud engine 与 encrypted KV 的端到端集成测试、space 删除后的密文清理验证仍需补充。
