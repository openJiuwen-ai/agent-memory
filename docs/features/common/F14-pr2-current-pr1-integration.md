# PR2 最新 PR1 基线适配与版本快照鉴权

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-08 |
| 影响范围 | API PEP、接入层兼容测试、PR2 Python 风格、S02 / S10 与 API AGENTS.md |
| 测试基线 | 3174 passed、11 skipped、26 warnings；排除外部存储集成，见「验证」 |
| Refs | #143 |

## 背景

PR1 已同步上游 MCP 全参数暴露、`as_of` 透传以及 Ruff UP 规则。PR2 旧提交直接叠加会
在参数声明、身份迁移测试和类型导入处冲突。更新不能恢复 legacy 自述身份，不能删掉
上游新增的工具参数，也不能把正在另一个工作区开发的 PR3 带入本 PR。

## 决策

1. 在独立 worktree 将 PR2 唯一实现提交重放到当前 PR1；保存旧 PR2 的本地备份引用，
   不改动原 `sec/audit` 工作区。PR1 原提交保留，PR2 的代码、测试、文档仍合为一个提交。
2. MCP 保留当前 PR1 的完整工具实现，经共享 `api_contract.invoke_api` 直调，不回退到
   legacy dispatch。`submit_ingest` 保留五个必填与四个可选业务参数，`ctx` 不进入 Schema；
   `verify_audit` 只暴露既有冻结接口，未装配 provider 仍返回 `unsupported`。
3. 历史 dispatch 测试通过真实认证上下文显式传入必填 `security`，不恢复旧 adapter 的
   缺省身份路径。保留 ROOT 开发身份经真实 Authorizer、在线凭据复核及私有代理绑定。
4. `get(as_of)` 保留请求 ID 的预检与真源权限检查，取数后再用最终返回的 MemoryUnit
   快照判权。当前 ID 可读并不意味着历史版本可读；即使 ID 没变，预读和返回之间的
   内容变化也不能继承旧权限。`get` / `inspect` / `trace` 共用私有快照权限投影函数，
   不修改公开 MemoryAPI、Authorizer、Store 或安全值对象。
5. PR2 类型导入与 UTC 写法遵守上游 UP 规则；原门禁的局部私有访问例外继续保留，
   新增故障注入测试仅在函数内说明例外，不新增公共调试接口或全局关闭检查。
6. 同步 S02、S10、API 模块说明以及 F05 的过时过渡描述。空间类型清单按现有代码
   补齐 owner / owners 和内容、治理两轴字段；这是文档纠偏，不是新增空间协议。
   F13 保留历史验收数据，并指明 MCP 当前参数集合已经随上游扩展。
7. 按既往两批门禁报告补验。测试 fixture 的 Python 定义使用私有名称，并显式注册原
   fixture 名称，消除新增的变量遮蔽告警而保持注入名称和生命周期。MCP Schema 断言
   使用 `get` 与唯一缺失哨兵，缺条目、缺属性、缺 default 都明确失败，合法 None 不
   当作缺失；必填字段集合使用显式循环，避免新增多行推导式风险。

## 拒绝的方案

- 合并 PR3 分支或覆盖原工作区：会混入本次范围外的实现及未提交文档整理。
- 为减少冲突整文件采用旧 PR2 MCP：会丢失上游 `as_of`、元数据等完整扁平工具参数。
- 只校验请求 ID、用第二次查询权限信息替代返回快照：跨版本或并发变化后权限可能错位。
- 用 `as_of` 扩展冻结的权限查询接口：PEP 已持有最终返回的同一快照，无需改变公开契约。
- 为通过新测试恢复可选 `security` 或 legacy actor：测试应适配可信上下文，而非放宽身份边界。
- 拆成额外 PR2 修复提交：实现 PR 最终保持一个完整提交，PR1 保持独立。
- 为消除 fixture 遮蔽而关闭规则、删除参数或弱化 Schema 相等断言：显式注册原
  fixture 名称即可解决定义冲突，契约缺项仍应使测试失败。

## 验证

- 历史版本漏洞修复前专项：4 failed、8 passed，分别复现 memory_type / pipeline 路由
  绕过，以及前向、后向版本选择的跨作者读取。修复后原专项与 handler 迁移测试 52 passed。
- 新增同 ID 返回快照变更拒绝测试；合法同作者的前向、后向版本读取仍成功。
- Python 3.12.7 / 已安装 MCP SDK，运行 `pytest tests/unit tests/integration
  --ignore=tests/integration/storage -q -rs -p no:cacheprovider -o addopts=''
  --basetemp=<独立临时目录> --tb=short --show-capture=no`：
  **3174 passed、11 skipped、26 warnings，150.74 秒**，无 xfail。PYTHONPATH 指向本 worktree
  根目录及 `jiuwen_memory_entry/core`；确认没有导入旧 PR2 / PR3 工作区源码。
- 11 项跳过：torch 两项、HanLP 模型两项、真实 LLM 四项、真实 Redis 双实例一项、
  Elasticsearch / Milvus 可选依赖各一项。HTTP、MCP、CLI、代理代写及安全专项均未排除。
  26 条警告为五条既有 asyncio marker 未注册与 21 条旧 security.local 配置弃用提示。
- 首轮扩大回归有一项 Windows 回环连接 `WinError 10053`；未修改测试或增加重试。
  HTTP 模块单独复跑 **67 passed**，同范围完整复跑获得上述全绿结果。首轮完整 unit
  发现的旧“两段鉴权次数”断言已改为明确核对新增返回快照判权，未删除安全断言。
- Ruff 0.16.10 全仓 `check` 通过，PR2 差异的 98 个 Python 文件 `format --check` 通过；
  `git diff --check` 通过。后续门禁复验另行隔离安装并直接执行 CI 固定的 Ruff 0.16.8。
- MemoryAPI 公共方法签名经 AST 比较与当前 PR1 一致；冻结的 Authorizer / Store /
  scope_rules / security.types 及 PR3 audit_integrity / ProtectedAuditLogger 文件无差异。
  MCP 工具实现也与当前 PR1 完全一致，未因消解冲突丢失上游工具或参数。
- 后续门禁复验：依据本地归档的两份 PR2 门禁核实报告（67 条与 18 条），扫描 PR2
  相对当前 PR1 的全部 98 个 Python 差异文件。Pylint 4.0.8 检查 protected-access、
  inconsistent-return-statements、super-init-not-called 与 too-many-boolean-expressions
  （max-bool-expr=3）；改动测试另检参数数量与变量遮蔽，不全局关闭规则。
  AST 差异检查覆盖相对旧 PR2 的 229 个 Python 文件、339 个变化定义；检查新增参数形状、
  单行定义、海象表达式、多行推导式，忽略仅类型注解现代化的既有参数形状。
- 门禁修复最终结果：上述 Pylint / AST 检查通过；四个专项测试文件的参数数量、变量
  遮蔽及解包检查通过。直接执行隔离目录中的 Ruff 0.16.8 可执行文件，全仓 check 与
  98 个 PR2 差异文件 format --check 通过。相关独立验收、可信上下文、handler、MCP
  工具和 MCP 传输测试 **314 passed、0 skipped，5.98 秒**。本轮只改测试声明、断言及
  本页，没有重跑前轮 3174 项扩大套件；前轮结果保留为基线，不计作本轮执行。

## 已知遗留

外部数据库集成、真实 ML 模型与 LLM 服务仍需相应部署环境验证。专有门禁平台未重扫，
本地 Ruff 与 pytest 不替代华为专有规则结论。动态委托管理公开接口、PR3 审计完整性实装、
FS 加密不在本次范围。新历史版本检查采用保守边界：请求条目与选中版本必须都可读。
