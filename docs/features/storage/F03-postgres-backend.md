# F03 — PostgreSQL KV 与 pgvector 存储后端

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-28 |
| 影响范围 | `jiuwen_memory/storage/`、`deploy/docker/postgres/`、`scripts/pg_schema.sql` |
| 测试基线 | `tests/unit/storage` 53 passed；PostgreSQL 真库测试由 `AGENT_MEMORY_TEST_PG_DSN` 启用 |
| Refs | — |

## 背景

现有生产部署分别使用 Redis 承载 KV 真源、Milvus 承载默认/L0/L1 三层向量索引。
希望在不改变 Store 接口和上层装配的前提下，用一个 PostgreSQL 实例替换这两个后端，
降低部署组件数量，并提供可直接拉起依赖服务的独立 Compose profile。

初版以快速可用为目标，实现深度对齐既有 Redis/Milvus 后端，不把迁移器、共享连接池、
大规模 ANN 调优和并发压测纳入交付门槛。

## 决策

1. 新增 `target: postgres` 的 `PostgresKVStore` 与 `target: pgvector` 的
   `PgVectorStore`，保持 `KVStore` / `VectorStore` 公共接口不变。
2. 使用同步 `psycopg` 3 和每个 Store 实例自有的惰性 `ConnectionPool`。未安装依赖时，
   import 和工厂注册仍成功，首次访问后端才抛 `BackendError`。
3. PostgreSQL 版本基线为 16；pgvector 最低 0.8.0。自动初始化在统一 advisory lock
   内按“创建扩展 → 校验版本 → 建表/索引”执行；独立 Compose profile 固定使用
   `pgvector/pgvector:0.8.3-pg16`，并由容器初始化脚本预建结构。
4. KV 用 scope 五列加 key 的复合主键；TTL 使用数据库时钟和 Unix 秒。过期行读时过滤，
   初版不内建清理调度。
5. Vector id 在完整 scope 内唯一，scope 五列加 id 组成复合主键。`update` 只更新
   embedding/metadata，scope 不符视为缺失，不允许借 update 迁移 scope。
6. 默认 HNSW + COSINE，同时保留 L2/IP 高分归一和 `index_type=none`。none 模式在查询
   事务内关闭 index scan，保证已有 HNSW 索引时仍执行精确搜索。
7. `FilterExpr` 完整下推到 jsonb SQL：`EQ` / `IN` 只匹配标量，
   `CONTAINS` 通过 `jsonb_typeof(...)= 'array'` 只匹配数组成员，`NE` / `NOT_IN`
   按对应正向谓词取反；缺失 key 与数值类型语义保持一致。值和 scope 全部参数化，
   schema/table/index 使用安全标识符。
8. 实现 `recall`：与 `search` 共用同条 KNN SELECT（同一套 HNSW 调优 + scope/filters
   下推），仅在 SELECT 列追加 `metadata` 一次回带——pgvector 本就一条 SELECT，
   合并"召回 + 取 payload"比远端后端（milvus）更直接，省去调用方再 `get` 的往返。
   `output_fields` 仅认 `"metadata"`，其余值忽略并记日志。
9. 新增独立 `postgres` profile，以 online 配置为基线，只替换 KV 与三层 Vector；
   Compose 同时启动 PostgreSQL/pgvector 与 Elasticsearch，应用等待两者健康后启动。
10. `agent-memory` 镜像的构建阶段以 root 安装依赖，运行阶段固定切换为
    UID/GID `10001:10001`；应用源码、bootstrap 与配置只读挂载，不依赖 root 权限。

## 拒绝的方案

- **同时迁移 Fulltext/Fusion 到 PostgreSQL**：会扩大到更多 Store 契约，首版继续使用
  Elasticsearch。
- **引入数据库迁移框架**：当前只有固定初始 schema，提供可执行 DDL 和幂等自动建表即可。
- **多个 Store 共享连接池**：需要额外的生命周期与装配治理；首版按实例持池，部署侧核算连接数。
- **运行时自动升级 pgvector**：部署环境权限和可用版本受控，运行时只校验下限，不执行
  `ALTER EXTENSION UPDATE`。
- **完整参数范围和索引结构审计**：与既有生产后端深度不匹配，非法数值由后端报错，
  索引治理留到出现实际需求后。

## 验证

- 工厂注册、必填参数、metric/index 白名单和 PostgreSQL FilterExpr 编译由离线单测覆盖。
- 真库集成测试覆盖 KV CRUD/TTL/scope、Vector 原子冲突/update scope、过滤、排序、
  `recall` 一次回带 metadata（与 `search` 同源、scope 隔离、filters 下推、未知
  `output_fields` 忽略）及预存 HNSW 下的 none 精确模式；无 `AGENT_MEMORY_TEST_PG_DSN`
  时按既有约定 skip。
- `postgres` Compose 包含应用、PostgreSQL/pgvector 和 Elasticsearch，不包含 Redis、
  Milvus、etcd 或 MinIO；初始化 DDL 只在空 PostgreSQL 数据卷首次启动时执行。
- 应用镜像构建、Compose 配置解析和非 root 容器冒烟通过；默认启动命令在只读源码与
  配置挂载下可启动 HTTP 服务并通过 `/healthz`。

## 已知遗留

- 不提供 Redis/Milvus 存量迁移、双写、切流和回滚工具。
- 不提供过期 KV 定时清理器、共享池、schema 版本迁移器或 `ivfflat`。
- 不做批量 update 的统一加锁顺序、多进程 schema 竞态压测和大规模 ANN recall/SLO 基准。
- `relaxed_order`、HNSW 参数自动调优及 pgvector 版本 warning 分层按真实负载后续评估。
