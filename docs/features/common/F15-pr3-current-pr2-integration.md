# PR3 当前 PR2 基线适配

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-08 |
| 影响范围 | PR3 审计完整性实现、API 兼容回归、MCP 工具回归、S07 / S10 与模块说明 |
| 测试基线 | 扩大回归 3338 passed、11 skipped；最新 XLSX 门禁整改专项 197 passed；检查范围见「验证」与「门禁报告后续整改」 |
| Refs | #143 |

## 背景

PR2 已基于最新 PR1/上游更新，包含完整 MCP 工具参数、`as_of` 透传、实际返回版本快照
判权和 Ruff UP 门禁。PR3 仍叠加在旧 PR2 上；直接保留旧树会丢失这些已完成的修复。
本次要求在 `sec/audit` 将既有审计完整性实现迁到当前 `sec/isolation`，继续遵守冻结接口。

## 决策

1. 保存旧 PR3 本地备份引用，将唯一 PR3 实现提交重放到当前 PR2。基线和提交数量检查
   确认 PR2 是直接父提交，PR3 仍为一个实现提交。PR1、PR2 分支保持原版本。
2. 内存审计后端的冲突合并保留链状态、互斥锁、二分有界扫描及普通审计过滤；导入和
   类型注解按当前上游使用 `datetime.UTC`、内置泛型及 `collections.abc`。
3. 保留当前 PR2 对最终返回 MemoryUnit 快照的判权以及 `_unit_snapshot_permission_context`
   投影。PR3 的 mutation 审计预检继续先于业务副作用；`get(as_of)` 不借审计适配回退授权。
4. MCP 实现完整继承当前 PR2，`verify_audit` 的四个业务参数继续经共享契约进入同一
   MemoryAPI。新增真实 SQLite provider 的工具回归，检验非默认增量起点、分页、样本预算
   与锚点策略；不替换认证器、Authorizer 或 provider。
5. 新增四项启用完整性后的跨版本组合回归：前向/后向版本选择 × 同作者/异作者。
   同作者仍可读，跨作者必须拒绝且拒绝事件落链，链验证应保持 clean。
6. 恢复迁移前未提交的 security-plans 归档及文档链接修改。保留原有删除/新增状态和本地
   stash 备份；这些改动不会混入 PR2，也不恢复旧报告路径覆盖归档文件。
7. 按之前两批门禁报告复核全部 PR3 Python 差异。fixture 定义使用私有名称，显式注册原
   注入名称；多行推导式改为局部变量或显式循环；proof 完整性条件分组，保留类型和正数
   校验。私有错误收集函数以一组状态旗标替代三个独立参数。篡改/故障注入仅在具体函数
   声明私有访问例外；provider 构造参数保持兼容，并在定义处说明参数数量例外。

## 拒绝的方案

- 整文件覆盖为旧 PR3：会回退当前 PR2 的快照授权、MCP 参数及上游类型规则。
- 合并两个实现提交：负责人约定每个实现 PR 一个提交；重放单提交即可维持栈结构。
- 放宽 MemoryAPI、Authorizer 或 key/store 契约来消解冲突：本次是实现兼容，不能新增或
  删除冻结接口。
- 对 Windows 回环连接失败添加测试重试或删除断言：保留原测试，定向复核并如实记录。

## 验证

- 相对当前 PR2，MemoryAPI、api 公共导出、AuditLogger、audit_integrity base / chain_store /
  包导出、KeyProvider、AuditEvent 共 8 组核心文件去除 docstring 后 AST 一致。
- MCP 工具实现文件与当前 PR2 完全一致；保留完整上游参数，没有回退为默认参数工具。
- Python 3.12.7、已安装 MCP SDK，最终完整单测及非外部存储集成：
  **3338 passed、11 skipped、22 warnings，169.67 秒，无失败、无 xfail**。
  命令为 `python -m pytest tests/unit tests/integration --ignore=tests/integration/storage
  -o addopts='' -q -rs -p no:cacheprovider --tb=short --show-capture=no`，使用独立 basetemp，
  JUnit 结果保存到本地 `security-plans/problems/2026-10-08-pr3-final-gate-regression.xml`。
  HTTP、MCP、历史版本判权及全部 PR3 审计回归均未排除。
- 本轮第一次扩大回归 **3326 passed、12 failed、11 skipped**；12 项均因环境缺 `jieba`。
  在仓库忽略目录隔离安装 `jieba==0.42.1`，经 PYTHONPATH 提供给 Python 3.12 后完整复跑
  得到上述结果，没有改产品代码、删除测试或放宽断言来处理缺依赖问题。
- 完整套件启动后，SQLite schema 的最后一处多行推导式做等价整理；最终文件状态的
  SQLite、detail 安全边界、新增 PR2 兼容及 MCP 专项 **151 passed，4.93 秒**。
- 11 项跳过为 torch 两项、HanLP 模型两项、真实 LLM 四项、真实 Redis 双实例一项、
  Elasticsearch 一项及已安装 Redis 时的缺依赖分支一项。22 条警告为既有 asyncio marker
  五条、旧 security.local 弃用十四条，以及 jieba 自身字符串转义三条。
- 之前暂停前的回归曾遇到 Windows 回环 `WinError 10053`；本轮两次完整执行的 HTTP
  模块均通过。新增组合测试初版的 Engine 和 audit 调用错误已修正，最终五项新增用例通过。
- 直接执行隔离安装的 CI 固定版本 **Ruff 0.16.8 可执行文件**：全仓 check 及全部
  **34 个 PR3 差异 Python 文件**的 check / format --check 通过，git diff --check 通过。
  全仓扫描仅有四项历史测试临时目录权限警告；源码、tests、scripts、examples、evaluation
  与 deploy 目录单独扫描通过且无访问警告。重放引入的 17 项 UP 问题及格式问题已修复。
- Pylint 4.0.8 复核 protected-access、inconsistent-return-statements、super-init-not-called
  和 too-many-boolean-expressions（max-bool-expr=3）通过；测试另检 too-many-arguments
  和 redefined-outer-name 通过。未全局关闭规则或新建公共调试接口。
  对生产代码额外启用参数数量规则会报 19 项 PR2 已有定义；逐项 AST 比较确认其参数
  形状未变，PR3 无新增未解释告警。这不表示已清除 PR2 的既有参数数量告警。
- 34 个文件的 AST 差异复核：无新增海象表达式、多行推导式、超过三项的布尔表达式、
  单行函数体或未解释的参数数量问题。冻结契约的 8 组 AST 比较仍通过。
- 详细复核记录保存在本地
  `security-plans/problems/2026-10-08-pr3-current-pr2-final-acceptance.md`；门禁 JSON 和工具
  保存在同一忽略目录，正式设计、实现、测试及既有四份报告归档统一纳入 PR3 的一个提交。

## 门禁报告后续整改

负责人提供 2026-10-08 17:32:42 导出的平台 XLSX，`代码问题详情(1)` 的 A1:S43
包含 42 项未解决问题。上次本地检查未覆盖此次文档字符串和内置名称规则，也没有完整
覆盖切片与测试代理的方法规则；此前本地通过不能证明平台没有剩余问题。

- 36 项 G.CMT.03：测试函数的多行文档字符串改为独立起止行，正文与结束引号均比函数
  声明缩进四格，保留原有安全场景描述；不通过删文档、改测试名称或规则例外处理。
- 3 项 G.NAM.04：私有事件样本函数的 `id` 参数改为 `event_id`，事件本身的 `id` 字段
  和全部调用语义不变。调用点原为位置传参，没有遗漏旧关键字参数。
- 1 项 G.FMT.04：内存链扫描先计算局部 `stop`，再使用 `[start:stop]`，与 Ruff 固定
  版本兼容；不关闭格式检查，仍保持相同的有界二分窗口。
- 2 项 G.CLS.07：并发测试的连接代理在实例上保存原连接，execute 与 __getattr__
  通过实例转发。__getattr__ 保持 Python 实例协议，等待事件、锁内事务时序与断言不变。

报告涉及的全部八个测试文件专项 **197 passed，6.78 秒，无跳过、无失败**；本次没有
重复前轮 3338 项扩大套件。9 个改动 Python 文件的 Ruff 0.16.8 check / format --check
与选定 Pylint 4.0.8 规则（增加 redefined-builtin）通过。42 行逐项本地结构复核通过，
冻结契约 AST 仍一致。原 XLSX 只读解析，未修改其问题状态；专有平台是否接受整改仍
以重新扫描为准。

详细逐行记录保存到本地
`security-plans/problems/2026-10-08-pr3-xlsx-gate-closure.md`，JSON 证据和 JUnit XML
保存在同目录。整改仍纳入现有 PR3 的单个实现提交，PR2 基线保持不变。

## 已知遗留

- 2026-09-23 的通过结果是旧基线的历史记录，不能替代本次新基线的回归结论。
- 本轮完整回归包含仓库现有 MCP 传输测试和工具到真实 SQLite provider 的调用链；外部
  存储后端、产品级外部锚点及生产库迁移仍需相应部署环境验证。
- 无外部锚点不能识别合法历史前缀回滚；LocalKeyProvider 轮换状态不跨重启保存。
- 华为专有门禁平台未在本轮重扫，本地规则复核不替代平台结论；必要私有访问及兼容
  构造参数的局部声明是否被平台认可，仍以平台重扫为准。
