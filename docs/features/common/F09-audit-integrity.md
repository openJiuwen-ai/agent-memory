# PR3 审计完整性（链式 HMAC 证明）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-22 |
| 影响范围 | `common/security/audit_integrity/`、`common/audit/audit_impl/`、`common/audit/protected_audit_logger.py`、`api/memory_api_impl/`（assembly / admin_ops / write_ops）、`jiuwen_memory_entry/core/auth_middleware.py`、`api/__init__.py` |
| 测试基线 | 2026-09-23 全量单测 + PR3 集成：3126 passed、12 skipped；补充归档 23 项正式回归全过；第七轮累计 52 项独立样本全过；MCP 实际传输与外部部署验证仍有边界 |
| Refs | #143 |
| 关联文档 | [F05 公共安全契约](F05-security-api-contracts.md)、[S10 安全规约](../../specs/S10-security.md)、[PR3 计划](../../../security-plans/archived/06-pr3-audit-integrity/plans/09-audit-integrity.md)、[独立验收记录](../../../security-plans/archived/06-pr3-audit-integrity/reports/2026-09-22-pr3-independent-acceptance.md) |

## 背景

PR1（认证 + 静态加密）与 PR2（授权 + 隔离）合入后，审计日志仍是普通追加：攻击者
（含能访问数据库文件的内部人员）可以改写、删除、重排历史审计事件而不被发现。PR3
按已冻结的 F05 §6 契约交付**审计完整性**：`ChainedHmacAuditIntegrityProvider` 为每条
事件生成版本化规范化的链式 HMAC 证明（每条 digest 覆盖前一条 digest），`verify_audit`
（`VERIFY_AUDIT` 管理面动作，根 scope 判权）流式验证全链。

PR 栈基于当前 PR2 `sec/isolation`；冻结核心契约（`base.py` / `chain_store.py` /
`MemoryAPI.verify_audit` 等）去除 docstring 后代码契约不变。复验 R7 指出的
`jiuwen_memory.api` 新增 `AuditIntegrityError` 导出已移除，公共导入面回到零增删。

2026-10-08 已将唯一 PR3 提交重放到更新后的当前 PR2，保留上游 MCP 全参数与返回版本
快照判权。下文 2026-09 的验收结果均为原基线历史记录；本次适配及验证边界见
[F15 当前 PR2 基线适配](F15-pr3-current-pr2-integration.md)。

## 决策

1. **链式 HMAC + 版本化规范化**：`canonical_event_bytes` 覆盖事件全部受保护字段、
   Scope 五维、UTC 时间与 detail 稳定排序（`sort_keys` + 紧凑分隔符），独立
   `CANONICAL_FORMAT_VERSION`；未知格式版本返回 `incomplete`、拒绝当 clean。MAC 用
   途标签 `audit-integrity:hmac:v1` 与信封加密派生互不复用；签发用活动 key、验证按
   proof 自带 KeyRef 选历史材料，不回退活动 key 试验。
2. **后端原子 CAS**：SQLite 以实例 RLock + `BEGIN IMMEDIATE` + busy_timeout 把
   「比较链头、插入事件+proof、推进 head」放进同一事务，跨连接/跨进程不分叉；CAS
   冲突由 provider 有界重试（默认 10 次），超限抛 `ChainConflictError`。sequence 由
   链头位置决定（`head+1`），不由 AUTOINCREMENT 分配。
3. **schema 四分（`_ensure_integrity_schema`）**：以 `PRAGMA user_version` 区分
   空库（建立 genesis）/ 旧无签名库（拒绝启动，`AuditMigrationRequiredError`）/
   合法现库（严格校验）/ 损坏库（`AuditSchemaError`）。完整性库（user_version=2）
   **禁止任何 DDL 修补**：初始化不做 ALTER 补列，核对全部核心事件列 + proof 列 +
   head 表；未知未来版本（>2）拒绝打开。`:memory:` 库按真实存储形态声明
   `persistent=False`（`is_test_only`），生产装配可拒绝。
4. **不修补证据**：proof 列缺失、format_version/key_epoch 非正整数时以空 digest
   sentinel 读出，provider 归 `incomplete`；其他非法类型仍可能在 Proof 构造时拒绝。
   NULL epoch 不再补成合法代次，避免 HMAC 检查被修补的材料。
5. **快照边界核对**（复验 R2/R4/R5 已修复）：SQLite `read_stable_snapshot` 核对
   head 与真实末行的全部字段（sequence/digest/key_id/key_epoch）与 genesis 固定值
   （`GENESIS_DIGEST`、空 key ref、当前格式版本）；health 持连接锁重验 schema 版本
   （`PRAGMA user_version`）与 head/末行一致性。内存后端同契约（`_check_head_locked`）；
   provider 对稳定快照再做 genesis 预检与 clean 扫描后的 head 复核兜底。
6. **增量验证与连续高水位**：`after_sequence > 0` 从稳定快照取 checkpoint，先验其
   proof 自洽再把 digest 作续链基线；checkpoint 不存在返回 `incomplete`，不回落
   genesis、不跳下一条。`high_water_mark` 是「本次**连续**成功验证到的最高
   sequence」，首个坏记录/缺口后冻结。
7. **外部锚点（可选）**：`AuditAnchor` 契约已冻结、provider 核对已实装——状态用
   `AnchorStatus` 枚举；本地链长于锚点（lagging）时必须核对锚定位置的本地前缀
   digest，不符即 `rollback_suspected`。产品级 WORM/KMS/云审计实现不在本期。
8. **接入 fail-closed**（R1/R7/R3 已修复）：装配以**实际 provider 为准**接线
   （内联组件配置同样安装 `ProtectedAuditLogger`），只有顶层段而无 Runtime 引用时拒绝
   启动。受保护写路径统一失败语义：`ProtectedAuditLogger` 把后端/密钥/序列化的任何
   失败归一为 `AuditIntegrityError` 并置 degraded 闩；认证入口按 `integrity_protected`
   标记对受保护审计的**任何**写入失败 fail-closed（普通审计的吞错语义保持不变、公共
   导入面零增删）。显式业务 mutation 入口已有
   degraded 闩 + `provider.health()` 预检，追加失败后闩保持至重建 Runtime；路由中的
   自动 fallback 建空间预检已提前到路由解析前（T1），不再在 degraded/损坏状态留下副作用。
   不构成业务库与审计库的跨库原子声明；`verify_audit` 的并发槽
   `guard.release` 覆盖包括尝试日志写失败在内的全部 acquire 后路径。
9. **轮换边界落链与 detail 写入过滤**（历史验收缺口已关闭）：以实际 proof 的
   KeyRef 判定轮换边界，局部签名/事件重试、CAS 冲突和成功轮换次数分别有界；正常最小
   预算、重复轮换及持续轮换耗尽样本均通过。wrapper 限制键长/键数/值长，保留系统键，
   在截断前做敏感词干匹配；非秘密标识仅精确允许 credential_id/key_fp，其他字段不因
   _id/_fp 后缀取得豁免（V1 已关闭）。该规则不等同于对任意值做秘密内容识别。

## 拒绝的方案

- **给 `AuditLogger` 基类塞链式方法**：默认空实现把「不支持」伪装成「支持但较弱」，
  fail-open；完整性保持独立 capability（`ChainedAuditStore`），装配期按 capability
  拒绝。
- **非对称签名（Ed25519 等）**：威胁模型是「篡改可检测」而非「可向第三方出示」；
  HMAC + 独立审计 key 已满足，避免密钥管理与验签成本。需要不可否认性时在锚点侧
  演进。
- **旧无签名库自动补签**：补签的历史等于「声称完整性的伪造历史」，直接拒绝启动，
  迁移走离线工具（见迁移确认）。
- **业务库与审计库跨库原子**：两库不是一个事务；预检 + 失败上抛已把不一致窗口
  收窄到「审计写失败」本身，该窗口由完整性错误显式暴露，不静默。
- **对完整性库做 DDL 自愈**（自动补列/重建 head 表）：会把「损坏」静默变成「看似
  合法」，攻击者删列即可重置校验基线。

## 最终验证

仓库 `.venv`（Python 3.13.9、ruff 0.15.17）。独立验收（2026-09-22）19 项故障注入
样本全部复现后逐项修复并沉淀为正式镜像测试；同日复验 10 项边界/真实故障测试（R1–R8）
由实现方报告修复，旧样本通过情况如下（第七轮独立结果见本节末尾）：

- 独立验收红队（`security-plans/archived/06-pr3-audit-integrity/redteam/test_pr3_acceptance_20260922.py`）：
  **19 passed**（修复前 19 failed）、复验红队（`test_pr3_reacceptance_20260922.py`）：
  **10 passed**（修复前 10 failed）；
- 验收范围回归（`tests/unit/common/security tests/unit/common/audit tests/unit/api
  tests/unit/jiuwen_memory_entry tests/integration/test_audit_integrity_flow.py` +
  红队，独立 basetemp）：**1682 passed, 1 skipped**（skip 为缺 `mcp` 依赖）；
- 首轮新增正式镜像测试（独立验收轮）：伪造 head（3 变体）、运行期 health 复核、
  未来 user_version、缺核心列重开拒绝（3 变体）、`:memory:` 非持久、key_epoch NULL、
  高水位冻结、锚点枚举状态（3 变体）/ 前缀不匹配 / 匹配 lagging / 不可用、guard
  写失败释放、内联配置接线、mutation 前健康预检、认证 fail-closed、末行 proof
  剥离拒绝、in_memory scan 有界边界；
- 复验轮新增正式测试：双后端 head/conformance（22，含参数化 5 组 + 反向样本）、
  SQLite head key_id/key_epoch 参数化 + genesis 固定值 + 临时数据库非持久 +
  health 持连接锁镜像 + 空路径临时表 + schema 版本运行期复核（共 7）、in_memory
  genesis 篡改 + head sequence + key ref 篡改拒绝（3）、provider 轮换边界自动落链
  + 不触发 + 连续轮换（3）、protected logger detail 脱敏 + 截断 + 键数上限 + 归一
  + 闩置位 + 闩粘性 + 标记区分（9）、认证中间件普通审计吞错保留（1）、集成
  authenticator 真实 SQLite trigger 阻断 + degraded 闩隔离与恢复 + update/delete/
  batch_add/evolve/grant 预检镜像 + detail 保留键防覆盖（8）；
- 改动文件 ruff：All checks passed。

### 第三轮修复与第四轮独立验收

第三轮的 12 项样本（T1×4、T2×2、T3×4、T4×1、T5×1）与此前 29 项本次全部通过。
第四轮范围回归实际为 **1693 passed、1 failed、1 skipped**，失败为 HTTP 用例的
WinError 10053，定向复跑通过；MCP 缺依赖仍为未验证边界，不能称作一次全量全绿。
第四轮新增 **4 failed**：重复轮换元数据不一致、最小 CAS 预算正常轮换失败、audit_key
漏滤、截断键名隐藏敏感词。T1/T4/T5 的原问题已关闭，T2/T3 尚未完整关闭。
改动 Python 文件 ruff 通过，整体验收仍未通过。详见
[第四轮报告](../../../security-plans/archived/06-pr3-audit-integrity/reports/2026-09-22-pr3-fourth-acceptance.md)。

### 第五轮独立验收

旧 45 项样本全部通过，范围回归 **1698 passed、1 skipped**，本轮没有 HTTP 连接失败。
新增 **3 failed**：持续轮换下缺总工作量预算、KDF 中间材料漏滤、非秘密 credential_id
被误删；两项问题延续第四轮的关闭标准，整体验收仍未通过。ruff 与接口 AST 检查通过。
详见 [第五轮报告](../../../security-plans/archived/06-pr3-audit-integrity/reports/2026-09-22-pr3-fifth-acceptance.md)。

### 第六轮独立验收

旧 48 项独立样本全部通过，范围回归 **1701 passed、1 skipped**。U1 总轮换预算已关闭；
字段规则补充 **2 passed、2 failed**：已知 credential_id/key_fp 正确保留，但未知敏感
扩展凭 _id/_fp 后缀同样获豁免。U2 仍有 V1 缺口，整体验收未通过；接口 AST 与 ruff 通过。
详见 [第六轮报告](../../../security-plans/archived/06-pr3-audit-integrity/reports/2026-09-22-pr3-sixth-acceptance.md)。

### 第七轮独立验收（2026-09-23）

本轮实现复验通过。累计 **52 项独立样本全部通过**，范围回归 **1705 passed、1 skipped**，
8 组冻结接口 AST、改动文件 ruff 和 diff 检查通过。V1 已改为非秘密字段精确允许列表，
已知标识保留与未知敏感扩展脱敏同时通过。未发现新的阻塞性实现问题。
MCP 实际传输因缺依赖未验证，全仓门禁未全部执行；正式测试归档、文档提交和单提交交付
仍待完成，不将本轮结论扩称为“全部最终交付门禁完成”。详见
[第七轮报告](../../../security-plans/archived/06-pr3-audit-integrity/reports/2026-09-23-pr3-seventh-acceptance.md)。

### 验收后的交付整理（2026-09-23）

上述“仍待完成”是第七轮验收时的状态；负责人随后授权整理、提交和推送。
后四轮的 23 项独立样本按模块归入正式 tests/：

- `tests/unit/common/security/audit_integrity/test_rotation_regressions.py`：5 项，覆盖签名期
  单次/重复轮换、边界 proof 与 detail 一致、最小 CAS 预算及总工作量预算。
- `tests/unit/common/audit/test_detail_security_regressions.py`：13 项，覆盖敏感字段、键长、
  截断前过滤、KDF 材料、已知非秘密标识及未知敏感后缀。
- `tests/integration/audit_integrity/test_write_boundaries.py`：5 项，覆盖隐式 fallback
  建空间前的 health/degraded 预检及满额 detail 的系统 request_id 保留。

正式测试不依赖被忽略的 redteam 文件。仅整理测试和文档，受验实现保持不变。
全量 `tests/unit` 与 PR3 两处集成测试共 **3126 passed、12 skipped**（140.01 秒），
其中上述 23 项全部通过；PR3 改动文件 ruff、补充测试格式及 diff 检查通过。
跳过的外部依赖/真实服务范围与交付命令见
[交付记录](../../../security-plans/archived/07-delivery-and-backups/reports/2026-09-23-pr3-delivery.md)。

## 迁移确认

无生产库待迁移（计划 §16 确认）：当前部署无保留的无签名审计库。启用完整性遇到旧库
时 `AuditMigrationRequiredError` 拒绝启动，报错指引备份/清库/离线迁移；离线补签工具
按计划判定为非必需，不交付。

## 已知遗留

- **无外部锚点时无法识别「合法历史前缀 + head 同步回滚」**：本地链式完整性只能
  检测内容修改与中间删除；防尾删/回滚需部署外部锚点（产品实现未交付）。
- **外部锚点无产品实现**：契约与 provider 核对已冻结并测试（fake anchor），WORM/
  KMS/云审计实现待后续。
- **MCP 传输实际运行未验证**：`memory_verify_audit` 工具存在且经 `invoke_api` 调用，
  但本环境缺 `mcp` 依赖，未做传输级验证。
- **detail 保护边界**：已实现敏感词干匹配、已知非秘密标识精确豁免和大小预算；V1 已关闭。
  服务器调用点仍须保证允许字段携带真实的非秘密标识，不承诺自动识别任意字段值中的秘密。
- **key 轮换状态不跨重启保留**（LocalKeyProvider 既有边界）；重复签名期轮换一致性、
  最小 CAS 预算及持续轮换的总工作量限制样本均通过，U1 已关闭。
- **普通 `record` 混入完整性库会被判 schema 损坏**（head 与末行不一致触发
  `AuditSchemaError`）：这是刻意的严格 fail-closed——两种模式不混用（计划 §4.1）。
- **验证范围**：第七轮 52 项独立样本全过，后四轮 23 项补充样本已归入正式 tests/；
  全仓外部服务集成及实际 MCP 传输不在本次通过声明内。代码、测试和文档统一纳入
  一个 PR3 提交，详见 [交付记录](../../../security-plans/archived/07-delivery-and-backups/reports/2026-09-23-pr3-delivery.md)。
