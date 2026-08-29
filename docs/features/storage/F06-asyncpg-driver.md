# F06 — PostgreSQL 驱动从 psycopg 迁移到 asyncpg

**日期**：2026-08-28
**状态**：已实施
**影响模块**：`jiuwen_memory/storage`（`_pg.py` / `kv_impl/postgres_kv.py` / `vector_impl/pgvector_vector.py`）

## 背景与决策

psycopg 3 为 LGPL-3.0，运行时分发引入源码可得/动态链接义务，与本项目分发合规
约束冲突。改用 asyncpg（Apache-2.0）。对外接口（方法签名、SQL 语义、返回值、
异常体系）完全不变。

## 核心方案

1. **同步→异步桥接**：asyncpg 是纯异步驱动而 Store 接口同步。模块级专职事件
   循环线程（`_LoopRunner`）常驻，同步方法经 `run_coroutine_threadsafe` 提交
   协程并阻塞等待；多调用线程并发提交，池并发不受影响。
2. **占位符策略**：`compile_pg_filter` / `pg_scope_clause` 继续产出 `%s` 片段
   （零改动），拼装完成后在执行边界 `_convert_placeholders` 统一改写 `$N`。
   本模块 SQL 不含字面 `%`（前缀匹配用 `starts_with` 而非 LIKE）。
3. **标识符引用**：`psycopg.sql.Identifier` 等价替换为 `_quote_ident`
   （`"` 翻倍转义）；DDL 中的 int 字面量经 `int()` 收敛后内插。

## Spike 实测基线（2026-08-28，PG 15.12 + pgvector 0.8.3 + asyncpg 0.31.0）

| 疑点 | 结论 |
|---|---|
| jsonb dict 直传 | 无 codec 报 `DataError`；注册 type codec 后 dict 直传直取，`::jsonb` 转型可省 |
| vector 绑定 | str + `$N::vector` 服务端转型即可，**无需** pgvector pip 包；`list[float]` 裸传报 `DataError` |
| `ANY($N::text[])` | Python `list[str]` 直传正常 |
| float → numeric | asyncpg 0.31 接受 float（返回 `Decimal`），无需手工 Decimal |
| bytea | 往返直接 `bytes` |
| `SET LOCAL` | autocommit 下静默失效（不报错）；须包显式事务 |
| `pg_advisory_xact_lock` | autocommit 下立即释放；schema DDL 须整体包事务 |
| 桥接并发 | 4 写 + 4 读线程 × 10 操作，40/40 成功、无死锁 |

## 拒绝的方案

- **psycopg LGPL 咨询/分发豁免**：合规义务不可控，直接排除。
- **pgvector pip 包**（register_vector）：str + `::vector` 已够用，拒绝新增依赖。
- **每次调用 `asyncio.run()`**：pool 绑定创建它的 loop，跨 run 复用崩溃。
- **实例持有 loop + `run_until_complete` + 全局锁**：所有调用串行化，并发退化。
- **`_ensure_schema` 不包事务**：advisory xact lock 在 autocommit 下立即释放，
  多实例并发建表存在竞态。

## 已知遗留

- 桥接 loop 线程为 daemon 常驻，进程退出即销毁；`close()` 只关池不停 loop。
- 未配置 asyncpg `command_timeout`（psycopg 时期也未配置），慢查询可挂住调用
  线程；如需再加。
- SSL 逃生舱语义不变：`ssl_verify=false` 时连接串自带 TLS 参数自理。

## 测试基线

- 单测：`tests/unit/storage/test_pg_driver.py`（新增）+ ssl/factory/late-binding
  改写用例全绿；全量 `tests/unit -m unit` 与迁移前一致。
- 集成：远端 pgvector 实例回归 10 passed
