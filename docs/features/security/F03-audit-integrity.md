# F03: 审计链式 HMAC 完整性保护

| 元数据         | 内容                                      |
|----------------|-------------------------------------------|
| **功能编号**   | F03                                       |
| **标题**       | 审计链式 HMAC 完整性保护                  |
| **状态**       | ✅ 已实现                                 |
| **日期**       | 2026-07-30                                |
| **负责人**     | MisterKnah                                |
| **关联 spec**  | S07（审计通用）                            |

## 问题

§7.2「安全审计」要求：「审计日志写入 SQLite（或集中审计服务）后，除追加外不可修改，防篡改。」
当前 `common/audit` 实现与 governor server 已落地审计记录但未防篡改--攻击者拿到 `audit.db`
磁盘访问后可直接修改行内容、删除行、或回滚到旧快照。

## 方案

在 `security/audit_hmac.py` 引入 `HmacAuditLogger` 装饰器，包装声明支持链式 CAS 的 `AuditLogger`
（`supports_chain_cas() == True`），`record` 时计算链式 HMAC 并存入 `event.detail["_hmac"]` /
`["prev_hmac"]`，`verify_integrity` 时重算并比对。构造时检查后端 CAS capability，不支持则
fail closed（审计 P1-1）。HMAC key 从 `LocalKeyProvider` 的加密根密钥派生（HKDF），与加密功能同源但派生隔离。

完整性逻辑住 `security/audit_hmac.py`，并发保护、事务 CAS、O(1) head 恢复、流式验证需要
后端原生支持，故扩展 `common/audit` 的 `AuditLogger` ABC 与 `SqliteAuditLogger`：
- 新增 `audit_chain_head` 表（`id=0` 单行），存 `head_hmac` / `last_seq` / `schema_version`
- 新增方法：`tail`（尾查询）、`record_chained`（CAS 链式写）、`get_chain_head`（O(1) 读链头）、
  `supports_chain_cas`（声明 CAS capability）、`get_chain_state`（原子快照）、`init_chain_head`
  （旧库迁移初始化）、`get_last_event`（启动校验）、`iter_chain`（keyset 分页）、
  `verify_integrity`（流式校验，返回 `AuditIntegrityResult` 结构化状态）
- 均有默认实现或空实现，普通后端不破坏

## 决策

### 决策 1：装饰器为主，扩展 common/audit 接口

`HmacAuditLogger(AuditLogger)` 包装声明支持链式 CAS 的 `AuditLogger`（`supports_chain_cas() == True`），
`record` 时算链式 HMAC 塞进 `event.detail["_hmac"]` / `["prev_hmac"]` 再委托，`verify_integrity()`
流式校验返回篡改行。构造时检查后端 CAS capability，不支持则 fail closed（审计 P1-1）。

完整性逻辑住 `security/audit_hmac.py`，但多实例事务 CAS、O(1) head 恢复、流式验证需要
后端原生支持，故**扩展了** `common/audit` 的 `AuditLogger` ABC（加 `tail` / `record_chained` /
`get_chain_head` / `supports_chain_cas` / `verify_integrity`，均有默认实现，普通后端不破坏）与
`SqliteAuditLogger`（加 `audit_chain_head` 表 + `BEGIN IMMEDIATE` 事务 CAS + `tail` DESC LIMIT +
`query` offset）。`InMemoryAuditLogger` 实现线程级 CAS（锁保护 expected_head 检查）。
这是与加密装饰器（`EncryptedKVStore`）的同构设计--装饰器管完整性逻辑，后端管持久化原子性。

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
- **原子快照**（审计 P1，第七次复验修复）：`get_chain_state()` 用单条 SQL 同时读取
  chain-head 和最后事件，使用 CTE 锚点确保查询永远返回一行。避免 head 行不存在时
  退回两次查询导致并发窗口（健康首次写入可插在中间）。

## 严格 schema 版本控制（审计 P1-2，第七次复验修复）

使用 SQLite `PRAGMA user_version` 作为权威版本标识（数据库级元数据，不受表操作影响）：

| user_version | head 表状态 | 判断 | 处理 |
|--------------|-------------|------|------|
| 0 | 缺失/不完整 | 真正旧库 | 添加列 → 迁移 |
| 1 | 迁移中 | 迁移中 | 继续迁移 |
| 2 | 完整存在 | 当前版本 | 正常运行 |
| 2 | 行被删除 | 损坏 | 拒绝启动 |
| 2 | 表被 DROP | 损坏 | 拒绝启动 |
| 2 | 列不完整 | 损坏 | 拒绝启动 |

**严格规则**：`user_version >= 2` 时，任何 head schema 缺失/降级都是攻击/损坏，一律拒绝。
只有 `user_version < 2` 的真正旧库才允许列迁移。

**防御效果**：能发现未同时修改版本标记的表损坏：
- DELETE head 行 → `user_version` 保持 2 → head 行不存在 → 拒绝
- DROP head 表 → `user_version` 保持 2 → 表缺失 → 拒绝
- DROP 后不完整重建 → `user_version` 保持 2 → 列不完整 → 拒绝
- 篡改 last_seq/head_hmac → `user_version` 保持 2 → 不一致检查 → 拒绝

**局限**：`user_version` 是 schema 迁移判别标记，但**不是抗篡改安全锚点**。拥有 SQLite 写权限
的攻击者可以执行 `PRAGMA user_version=0`，然后删除尾事件、DROP head 表，启动时会把剩余
合法前缀当旧库迁移。这与「尾删/回滚无法检测」属于同一已知局限（见下文），真正的防回滚/尾删
仍依赖外部可信锚点。

## get_chain_state 原子快照（审计 P1，第七次复验修复）

`get_chain_state()` 返回 `(head_hmac, head_last_seq, last_event_seq, last_event_hmac, schema_version)`，
用于启动一致性校验和旧库迁移判断。

**单条 SQL 原子快照**：
```sql
WITH anchor AS (SELECT 1 AS placeholder)
SELECT h.head_hmac, h.last_seq, e.seq, e.detail_json, h.schema_version
FROM anchor
LEFT JOIN audit_chain_head h ON h.id = 0
LEFT JOIN audit_events e ON e.seq = (SELECT MAX(seq) FROM audit_events)
```

**关键设计**：
- `FROM anchor` 确保查询**永远返回一行**，即使 head 和 events 都为空
- 双 LEFT JOIN 同时读取 head 和最后事件
- 列可能为 NULL，但 `row is None` 永远不会发生
- 消除了 head 行不存在时的 fallback 查询分支（避免并发窗口）

**并发保护**：健康首次写入可以插在两次查询之间，使用 CTE 单 SQL 后不再有并发窗口。

## AuditIntegrityResult 结构化状态（审计 P2-2）

`verify_integrity()` 返回 `AuditIntegrityResult` 对象而非裸列表：

```python
@dataclass(frozen=True)
class AuditIntegrityResult:
    status: str  # unsupported / clean / tampered
    tampered_indices: list[int] = field(default_factory=list)
    checked: bool = False  # 第三位（位置参数兼容）
    tampered_count: int = 0  # 实际篡改总数（审计 P3）
    samples_truncated: bool = False  # 采样是否被截断
```

**调用方据此区分**「已验证且干净」与「根本没有完整性保护」：
- `status="clean" + checked=True`：已验证无篡改
- `status="unsupported" + checked=False`：后端无完整性保护
- `status="tampered"`：检出篡改，`tampered_count` 是真实总数，`samples_truncated` 表示是否超过采样上限

采样上限（默认 100）防止大量篡改耗尽内存，`tampered_count` 和 `samples_truncated` 让调用方
了解真实损坏规模（100 个索引可能对应 100 个或数百万个篡改）。

**位置参数兼容性**：`checked` 保持在第三位（原位置），新字段追加在后，保证
`AuditIntegrityResult("clean", [], True)` 等旧式调用仍然有效。

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

- `tests/unit/security/test_audit_hmac.py`（35 条）：链式链接、改行不重算检出、重算后
  后续链断、伪造 prev_hmac 检出、干净链空、key 派生确定且隔离、错 key 全链判篡改、
  query 透明、包 SqliteAuditLogger、factory 装配、factory 要求 inner、factory 自引用拒、
  **并发不分叉**、**后端失败不推进链头**、**重启恢复链头**、**SQLite 跨实例续链**、
  **尾删已知局限**、**旧库迁移成功**（version 0 → 添加列 → version 2）、
  **当前库 DROP 表拒绝**（version 2 保持 → 拒绝）、**当前库不完整重建拒绝**（version 2 保持 → 拒绝）、
  **空事件 + 悬空 head 拒绝**、**last_seq 篡改拒绝**、**head_hmac 篡改拒绝**、
  **采样总数和截断标志**。
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

1. **verify 生产可达入口**：
   - 当前 `verify_integrity` 只在单测调用，缺 Governor/CLI/告警路径。启动验证已闭环
     「坏库拒启动」，但运行期主动验证（完整性巡检）无入口。
   - 需要：新 API endpoint `POST /system/audit/verify`（需 ROOT）、Governor 选项卡、
     CLI `agent-memory audit verify`、定期自动验证 + 告警。
   - 时机：独立安全功能增强任务。

2. **key 轮换与 epoch**：
   - 当前单 key，root key 轮换后旧链无法验证；缺 `key_id` / `epoch` / 历史 key 管理。
   - 需要：`AuditEvent.detail` 加 `key_id`，`HmacAuditLogger` 维护 `key_registry`，
     `verify_integrity` 根据 `key_id` 选 key。root key 轮换后保留旧 key 派生产物于内存/
     KMS，并记 epoch 边界。
   - 时机：root key 轮换机制成熟后。

3. **外部可信锚点**：
   - 本地链式 HMAC 无法检测尾删和回滚（见「已知局限」）。
   - 需要：周期性把 `(seq, chain_head, key_id, epoch)` 推送至 WORM / 远端 append-only
     审计服务 / KMS 签名后写入独立存储；启动时比对本地 head 与外部锚点，检出被回滚。
   - 时机：独立安全架构工程，需外部依赖评估 + 部署方案。

4. **流式验证内存优化**：
   - 当前 `verify_integrity` 返回全部篡改行索引（采样上限 100）。百万行 audit.db 中若
     前 50 万行全坏，内存占用仍然有限（只采样 100）。
   - 优化：改为生成器 `yield` 逐个篡改行，调用方流式处理或累积。当前已够用。
   - 时机：生产出现超大 audit.db + 验证耗时/OOM 场景。

5. **跨实例并发写保护**：
   - 当前 `record_chained` 用 SQLite 事务 + `BEGIN IMMEDIATE`，单机多线程安全，多实例
     写同一 SQLite 文件的锁竞争窗口极小但存在。
   - 优化：若部署需多实例共享 `audit.db`，改用集中审计服务（S3/Kafka/远端 RDBMS）。
   - 时机：部署架构确认需多实例写同一 SQLite 文件。当前架构不需要。
