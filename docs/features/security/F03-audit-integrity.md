# F03 - 审计链式 HMAC 完整性保护

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-30 |
| 影响范围 | **新增**：`src/security/audit_hmac.py`（`HmacAuditLogger` + `derive_audit_key` + `hmac` 注册）、`tests/unit/security/test_audit_hmac.py`、`tests/unit/api/test_audit_security_fields.py`<br>**修改**：`src/common/audit/base.py`（ABC 加 `tail`/`record_chained`/`get_chain_head`/`verify_integrity` + `query` offset）、`src/common/audit/audit_impl/sqlite_audit_logger.py`（`audit_chain_head` 表 + 事务 CAS + `tail` DESC + `query` offset + `init_chain_head`/`get_last_event`）、`src/common/audit/audit_impl/in_memory_audit_logger.py`（`tail` + `query` offset）、`src/common/factory/factory.py`（`_building` 依赖环检测）、`src/common/type_def/auth.py`（`AuthContext` 加 `auth_mode`）、`src/security/authenticator_impl/{api_key,dev,trusted}_authenticator.py`（填 `auth_mode`）、`src/security/key_store_impl/memory_key_store.py`（resolve 填 `auth_mode`）、`src/security/bootstrap.py`（import `audit_hmac`）、`src/api/memory_api_impl/assembly.py`（`_enforce_audit_integrity` 启动约束 + import audit_hmac）、`src/api/memory_api_impl/local_memory_api.py`（`_record_audit` 填四字段） |
| 测试基线 | 改动前 `15 failed, 884 passed, 1 skipped`；改动后 `15 failed, 897 passed, 1 skipped`。15 个失败为同一组环境依赖项 |
| 依据 | [`docs/features/common/F04-security-interfaces-and-encryption.md`](../common/F04-security-interfaces-and-encryption.md) §7.2 必须记录的事件、§7.3 日志记录的完整性保护 |

> **行文简称**：下文里的 **security.md** 一律指上表「依据」那份文档（详见 F01 同名说明）。
> PR①/② 落地了认证与授权，本期补审计完整性--三份文档是同一根线的三段。

## 背景

security.md §7.3 要求审计日志不能被篡改，链式 HMAC 让「改一行 = 破坏该行及后续所有行
的 HMAC」。§7.2 要求审计记录 `acting_user` / `role` / `key_fp` / `auth_mode` 四样认证
元数据。一期两者都未做（`AuditEvent.detail` 注释已登记「安全层另加四个」）。

第三次验收 PR③ 收口事项明确要求：HMAC 必须覆盖稳定规范化后的全部字段、不能用一种序列化
签名又用另一种验证、需定义并发写入/重启/轮换规则、要覆盖攻击成功与拒绝路径。

## 决策

### 决策 1：装饰器为主，扩展 common/audit 接口

`HmacAuditLogger(AuditLogger)` 包住任意 `AuditLogger`，`record` 时算链式 HMAC 塞进
`event.detail["_hmac"]` / `["prev_hmac"]` 再委托，`verify_integrity()` 流式校验返回篡改行。

完整性逻辑住 `security/audit_hmac.py`，但多实例事务 CAS、O(1) head 恢复、流式验证需要
后端原生支持，故**扩展了** `common/audit` 的 `AuditLogger` ABC（加 `tail` / `record_chained` /
`get_chain_head` / `verify_integrity`，均有默认实现，普通后端不破坏）与 `SqliteAuditLogger`
（加 `audit_chain_head` 表 + `BEGIN IMMEDIATE` 事务 CAS + `tail` DESC LIMIT + `query` offset）。
`InMemoryAuditLogger` override `tail`/`query` offset。这是与加密装饰器（`EncryptedKVStore`）
的同构设计--装饰器管完整性逻辑，后端管持久化原子性。

### 决策 2：只填 detail，不提升为一等字段

§7.2 的四个认证字段塞进 `AuditEvent.detail`（`dict[str, str]`），**不**提升为 `AuditEvent`
一等字段。提升要改 `common/type_def/audit.py` + `control` + 两个 `AuditLogger` 实现 +
`handler._event_view`，是跨模块破坏性变更。当前 `detail` 已稳定承载这四样（注释早有登记），
提升是独立的结构整洁度需求，不在本期。唯一新增的一等字段是 `AuthContext.auth_mode`
（见决策 4）--它是认证产物本身就该带的信息，不是审计结构变更。

### 决策 3：HMAC key 从 Encryption Root Key 派生

`derive_audit_key(root_key)` 用 HKDF（SHA256，info=`agent-memory:security:audit-hmac:v1`）
从 `LocalKeyProvider.get_encryption_root_key()` 派生。与加密根密钥同源但派生隔离：同 root
key 派生同 audit key；不同 root key 派生不同。

**已知局限**：派生 `info` 是公开常量，不构成安全门槛--root key 泄漏后攻击者可派生 audit key
重算任意链。完整解需独立 audit key/KMS（见遗留 2）。轮换根密钥会让历史链无法验证（当前无
`key_id`/`epoch`，见遗留 2）--这是当前范围的已知限制，非「那是对的」。不单独管理 audit key
是本期简化，完整 key 生命周期见遗留 2。

### 决策 4：`AuthContext` 加 `auth_mode` 字段

`AuthContext` 此前没有 `auth_mode`，`_record_audit` 无法直接取。加 `auth_mode: str = ""`
字段（默认空），各 authenticator 显式填（dev/trusted/api_key）。这是认证产物本身就该带的
信息（这次认证走了哪条路径），不是审计结构变更。4 个构造点（3 个 authenticator + key_store
resolve）都填上。

### 决策 5：配置驱动 opt-in 接线 + 启动约束

`audit.default.target: hmac` 时包一层（`hmac` 注册到 `AuditProducer`，与 `encrypted`
装饰器同构）。必须配 `inner`（被包的 audit logger）。`build_kernel` 直连路径不调
`register_security`，故 `hmac` 不在其注册表--直连路径（测试/quickstart）用不上 HMAC，
符合「HMAC 是部署级需求（防磁盘 audit.db 被篡改）」。真实部署走 `Server.build`（调
`register_security` 后 `build_kernel`），`hmac` 可用。

**启动约束**（决策 6 / PR③ HMAC 策略）：`build_kernel` 装配 audit 前调
`_enforce_audit_integrity`。策略与 auth_mode 解耦，由独立配置控制：

- **真文件持久化**（audit target=sqlite 且 db_path 非 `:memory:`/空）+ 未包 HMAC
  -> **拒绝启动**（`ValidationError`）。生产/生产仿真必须配 `target: hmac + inner`。
- **DEV + 内存审计**（in_memory 或 sqlite `:memory:`）-> 允许无 HMAC，保调试速度。
- **HMAC 单测/红队** -> 显式配 `target: hmac`。
- **安全集成/E2E** -> 至少一组 DEV + HMAC 共存验证。

不据 auth_mode 自动决定是否启用 HMAC--HMAC 由独立配置控制，避免「认证模式」与
「完整性保护」两个正交关注耦合。

## 链式 HMAC 规则

- 每条 `record`：`hmac = HMAC(key, prev_hmac + canonical(event))`，`prev_hmac` 初始空。
- `canonical(event)`：稳定 JSON 序列化（`sort_keys` + 固定分隔符），覆盖全部字段，排除
  `_hmac` / `prev_hmac`（防自指）；`Scope`（frozen）按字段展开；`datetime` 用 isoformat。
- `verify_integrity`：逐条重算并 `compare_digest` 比对，同时校验 `prev_hmac` 与前一条
  `_hmac` 一致。任一不符记为篡改。

§7.3「改一行 = 破坏该行及后续」的精确语义：攻击者改某行内容后，不重算 HMAC -> 该行检出；
重算该行 HMAC（需 key）-> 该行 `_hmac` 变 -> 下一条 `prev_hmac` 对不上 -> 检出。没有根
密钥就修不好，这才是链式 HMAC 的保证。

## key 派生与轮换

`derive_audit_key` 是确定性 HKDF。root key 轮换后：
- 新事件用新派生 key 算 HMAC，与旧链不同 key；
- `verify_integrity` 用旧 key 校验旧链、新 key 校验新链（本期 `verify` 用构造时传入的
  单一 key，跨轮换的混合链校验是遗留项）。

## 并发与重启

- **并发写入**：`HmacAuditLogger.record` 用锁覆盖完整的「读链头 -> 算 HMAC -> 委托追加
  -> 更新链头」区间（审计 P1-1）。`SqliteAuditLogger` 的内部 RLock 只保护单次 SQLite 写，
  保护不了装饰器外层已发生的链头读取，故装饰器自带锁。后端写入失败不推进链头，避免
  链头与后端不一致。
- **进程重启**：构造时从持久化后端读最后一条的 `_hmac` 作为 `_prev_hmac` 初值
  （`_recover_chain_head`，审计 P1-2），续接旧链而非从空开始。正常滚动发布/崩溃恢复/
  机器重启不再被误判篡改。

## 已知局限：尾删与回滚不可检测（审计 P1-3）

本地链式 HMAC 能检出「中间内容被改且攻击者没有 key」，但**无法**检出以下两类攻击--

- **删除日志尾部**：攻击者完成敏感操作后删掉含该操作的尾部行，剩余记录的 HMAC 仍全部
  自洽，`verify_integrity` 返回空。
- **数据库回滚到旧快照**：回滚到过去任一合法前缀，剩余事件 HMAC 全部正确。

这是本地链式 HMAC 的密码学本质局限，不是实现缺陷。完整防篡改必须引入**本地数据库之外
的可信状态**（WORM/远端 append-only 审计服务、独立受控存储、KMS/HSM 签名的检查点并异地
保存），周期性把 `(seq, chain_head, key_id, epoch)` 锚定到攻击者不能同时回滚的位置，启动
与完整性检查时比对本地 head 与外部锚点。这是独立的安全架构工程，不在本期范围。

**本期明确不宣称**「完整审计防篡改能力」，只宣称「中间内容篡改检测」。`test_tail_truncation_
is_known_limitation` 钉住此行为，未来引入外部锚点后该测试应改为检出。

## 测试

- `tests/unit/security/test_audit_hmac.py`（34 条）：链式链接、改行不重算检出、重算后
  后续链断、伪造 prev_hmac 检出、干净链空、key 派生确定且隔离、错 key 全链判篡改、
  query 透明、包 SqliteAuditLogger、factory 装配、factory 要求 inner、factory 自引用拒、
  **并发不分叉**、**后端失败不推进链头**、**重启恢复链头**、**SQLite 跨实例续链**、
  **尾删已知局限**。
- `tests/unit/api/test_audit_security_fields.py`（3 条）：有 AuthContext 填四字段、无
  AuthContext 不填、ROOT+DEV 模式。
- `tests/unit/api/test_build_kernel_config.py`（5 条约束）：真文件 sqlite 无 hmac 拒、
  `:memory:` 允许、in_memory 允许、hmac+sqlite 允许、无 audit 配置允许（DEV）。

## 范围降级声明（实现侧建议，待负责人确认，审计 P2-2）

> 审计同事指出：实现同事不能用自己写一句「负责人批准」替代验收授权。本降级是**实现侧
> 建议**，需负责人在本任务中明确确认或引用可追溯决策记录。在此之前的验收应保持待决。

PR③ 本期只交付：**单 key、多实例（事务 CAS）、本地中间行篡改检测 + 启动验证 + chain-head
一致性**。

明确**不**包含（需独立 PR，待负责人确认范围）：
- 尾删/回滚检测--需外部可信锚点（WORM/远端/KMS 检查点），本地链式 HMAC 密码学本质局限；
- key 轮换 epoch--需 `key_id`/`epoch` + 历史key保留 + 独立 audit key 生命周期；
- verify 生产可达入口（Governor/CLI/告警）--启动验证已闭环「坏库拒启动」，但运行期主动验证入口待落地。

外部锚点与 epoch 是独立安全架构工程。若负责人不批准降级，需回到 checkpoint + key epoch 实现。


## 遗留

1. **外部可信锚点**（审计 P1-3）：尾删/回滚检测，需 WORM/远端/KMS 检查点架构，不在本期（见范围降级）。
2. **key 轮换与 epoch**（审计 P2-3）：每条/每段记 `key_id`/`epoch`，保留历史 key 验证旧链，
   独立 audit key/KMS 生命周期。当前 root key 泄漏即可重算链，轮换让历史不可验证。
3. **verify 生产入口**（审计 P1-2 余下）：启动验证已实现（坏库/未签名库拒启动）；`verify_integrity`
   已提升为 `AuditLogger` 接口（返回 `AuditIntegrityResult` 结构化状态，默认 `unsupported`，
   审计 P2-2）；`iter_chain` keyset 分页已实现（审计 P2-1）；运行期入口（Governor/CLI/告警）待落地。
4. 四字段提升为 `AuditEvent` 一等字段（独立结构整洁度需求）。
