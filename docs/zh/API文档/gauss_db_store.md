# foundation.store.db — GaussDB 适配模块功能说明

本文档描述 `foundation/store/db/` 目录下两个文件的实现功能、对外接口、约束条件，以及在 `LongTermMemory` 长期记忆引擎中接入 GaussDB 的完整使用样例（涉及表创建与数据增删改查）。

涉及文件：

| 文件 | 作用 |
|------|------|
| `gauss_db_store.py` | 提供 `GaussDbStore` 数据库存储实现，封装 SQLAlchemy `AsyncEngine` 供长期记忆引擎使用 |
| `gauss_dialect.py` | 注册 GaussDB 自定义方言 `gaussdb`/`gaussdb.async_gaussdb`，对 `async_gaussdb` 驱动做 DBAPI 兼容补丁，并修复 `pg_type` 反射 SQL |

---

## 1. 设计方案

### 1.1 架构设计原则

`GaussDbStore` 的设计遵循以下核心原则：

1. **最小化依赖**：只依赖标准 SQLAlchemy 接口，避免与特定数据库强耦合。
2. **隔离性**：不污染全局命名空间（不修改 `sys.modules`），与其他数据库驱动并存。
3. **扩展性**：基于 SQLAlchemy 方言机制（`registry.register` + `import_dbapi`），易于维护和升级。
4. **标准兼容**：遵循 Python DB-API 2.0 与 SQLAlchemy 最佳实践。

### 1.2 方言实现方案

`async_gaussdb` 与 `asyncpg` 接口高度相似，但并不是同一个包。要让 SQLAlchemy 把 `async_gaussdb` 当作 PostgreSQL 异步驱动来用，有两种思路：

**思路一（已废弃）：在程序启动时偷偷把 `async_gaussdb` 顶替成 `asyncpg`**

```python
# 直接修改 Python 的全局模块表，让所有 import asyncpg 的代码
# 实际拿到的都是 async_gaussdb
import async_gaussdb, sys
sys.modules['asyncpg'] = async_gaussdb
sys.modules['asyncpg.exceptions'] = async_gaussdb.exceptions
```

这种做法会带来以下问题：

- **全局副作用**：进程内任何一处 `import asyncpg` 都会被改写，影响其他真正使用 `asyncpg` 的代码（例如 SQLAlchemy 自己的错误翻译模块）。
- **行为不可预测**：替换发生在导入阶段，调用方很难察觉，调试时难以定位问题。
- **不符合 SQLAlchemy 规范**：SQLAlchemy 官方提供了方言扩展点用于此类需求，绕过它意味着升级 SQLAlchemy 时容易出现兼容性问题。

**思路二（当前实现）：通过 SQLAlchemy 官方扩展点 `import_dbapi()` 局部加载驱动**

```python
class GaussDialectAsyncpg(PGDialect_asyncpg):
    @classmethod
    def import_dbapi(cls):
        import async_gaussdb
        patched_driver = _patch_gaussdb_driver(async_gaussdb)
        return AsyncAdapt_asyncpg_dbapi(patched_driver)
```

`import_dbapi()` 是 SQLAlchemy 方言体系中规定的钩子方法：方言被使用时，SQLAlchemy 会调用此方法获取真正的数据库驱动模块。这种方式只在方言内部加载 `async_gaussdb`，并通过 `AsyncAdapt_asyncpg_dbapi` 适配为 SQLAlchemy 期望的异步接口，不会影响进程内其他模块。优点：

- **无全局副作用**：不修改 `sys.modules`，与其他依赖 `asyncpg` 的代码完全隔离。
- **符合 SQLAlchemy 规范**：使用官方扩展点，行为稳定、易于维护。
- **结构清晰**：驱动加载逻辑集中在 `GaussDialectAsyncpg` 类内部，职责单一。

#### 为什么继承 `BaseDbStore` 而非 `DefaultDbStore`

虽然 `GaussDbStore` 与 `DefaultDbStore` 当前实现几乎相同，但仍直接继承 `BaseDbStore`，原因如下：

1. **语义清晰**：`BaseDbStore` 是抽象基类，定义统一存储接口；`GaussDbStore` 明确表达"GaussDB 专用实现"。
2. **模块隔离**：核心模块保持纯净，GaussDB 专用实现位于 `foundation/store/db/`，可独立安装、独立维护。
3. **未来扩展**：后续可加入 GaussDB 特有的连接池配置、监控指标、性能优化，互不影响。
4. **依赖管理**：`async_gaussdb` 作为可选依赖，未安装时不影响默认存储链路。

### 1.3 驱动补丁机制（`_patch_gaussdb_driver`）

由于 `async_gaussdb` 缺少部分 DBAPI 标准属性，导入时会自动补齐：

```python
def _patch_gaussdb_driver(driver_module):
    if not hasattr(driver_module, 'paramstyle'):
        driver_module.paramstyle = 'format'
    if not hasattr(driver_module, 'Error'):
        driver_module.Error = getattr(driver_module, 'GaussDBError', Exception)
    if not hasattr(driver_module, 'apilevel'):
        driver_module.apilevel = '2.0'
    if not hasattr(driver_module, 'threadsafety'):
        driver_module.threadsafety = 0
    return driver_module
```

确保驱动符合 Python DB-API 2.0 规范，使 SQLAlchemy 能正确识别和使用该驱动。

### 1.4 反射 SQL 修复（`_patch_gaussdb_reflection_sql`）

`PGDialect_asyncpg` 在反射元数据时会查询 `pg_type.typcollation`，GaussDB 不完全兼容此子查询。`gauss_dialect.py` 通过 `before_cursor_execute` 事件钩子，将 `(SELECT … pg_type.typcollation …)` 替换为 `NULL`，使得 `Base.metadata.create_all`、`inspect()` 等元数据操作可以在 GaussDB 上正常执行。

### 1.5 字符串绑定处理（`GaussString`）

`GaussString` 继承 `String`，在 `bind_processor` 中保证：

- `None` 透传不处理。
- `datetime.datetime` 自动按 `%Y-%m-%d %H:%M:%S.%f` 格式化为字符串。
- 其他非字符串类型统一 `str(value)`。

避免直接传入非字符串数据时驱动报错或写入异常。

### 1.6 其他方言定制

- `GaussCompiler.for_update_clause`：将 `SELECT ... FOR UPDATE` 编译为标准 `FOR UPDATE`，避免 PG 特有 `OF`、`SKIP LOCKED` 子句生成。
- `GaussDialectAsyncpg._get_server_version_info` 固定返回 `(9, 2)`，绕开 GaussDB 版本号解析差异。
- `_domain_query` / `_enum_query` 返回 `SELECT 1 WHERE FALSE`，跳过 PostgreSQL 特有的 domain/enum 反射，提升反射兼容性。
- `supports_native_enum=False`、`supports_native_uuid=False`、`use_insertmanyvalues=False`，关闭 GaussDB 不完全支持的 PG 特性。

---

## 2. 调用链路

```
from foundation.store.db.gauss_db_store import GaussDbStore
  │
  ▼
gauss_db_store.py → import gauss_dialect            # 模块加载，仅执行一次
  │
  ▼
gauss_dialect.py 模块加载时自动完成：
  ├── registry.register("gaussdb", ...)             # 注册 GaussDialectAsyncpg
  ├── registry.register("gaussdb.async_gaussdb",...)
  └── event.listens_for(Engine, "before_cursor_execute")  # 反射 SQL 修复
  │
  ▼
create_async_engine("gaussdb+async_gaussdb://...") 触发：
  └── GaussDialectAsyncpg.import_dbapi()            # 局部加载 async_gaussdb 驱动
  │
  ▼
GaussDbStore(async_conn=engine)                     # 创建存储实例
  │
  ▼
store.get_async_engine()                            # 获取 AsyncEngine → 通过 SQLAlchemy 执行 SQL
```

> Python 模块缓存机制保证 `gauss_dialect.py` 中的初始化逻辑只执行一次，无需用户手动调用。

---

## 3. 公开接口

### 3.1 `class GaussDbStore(BaseDbStore)`

```python
class foundation.store.db.gauss_db_store.GaussDbStore(async_conn: AsyncEngine)
```

GaussDB 数据库存储实现，继承自 `BaseDbStore`。封装 SQLAlchemy `AsyncEngine`，上层业务通过 `get_async_engine()` 获取引擎后即可使用标准 SQLAlchemy 异步接口执行增删改查。

导入此模块时会自动触发 `gauss_dialect` 注册（通过 `import_dbapi()` 局部加载驱动），无需用户手动调用。

**参数**：

- `async_conn` (`AsyncEngine`)：SQLAlchemy 异步引擎实例。

**样例**：

```python
from sqlalchemy.ext.asyncio import create_async_engine
from foundation.store.db.gauss_db_store import GaussDbStore

engine = create_async_engine("gaussdb+async_gaussdb://user:password@host:port/database")
store = GaussDbStore(async_conn=engine)
```

#### `get_async_engine() -> AsyncEngine`

返回内部持有的 `AsyncEngine` 实例（多次调用返回同一实例）。

```python
engine = store.get_async_engine()
assert engine is store.get_async_engine()
```

#### 属性 `async_conn: AsyncEngine`

内部持有的 `AsyncEngine` 实例。

### 3.2 `gauss_dialect.py` 公开方言名

| 方言名 | 用途 |
|--------|------|
| `gaussdb` | 默认 GaussDB 异步方言别名 |
| `gaussdb.async_gaussdb` | 显式指定使用 `async_gaussdb` 驱动的方言 |

连接字符串格式：

```
gaussdb+async_gaussdb://username:password@host:port/database
```

或简写：

```
gaussdb://username:password@host:port/database
```

---

## 4. 使用限制（约束条件）

### 4.1 支持的功能

- ✅ 标准 SQLAlchemy Core 与 ORM 操作（CRUD、事务、连接池、异步查询）。
- ✅ PostgreSQL 兼容语法的大部分数据类型与函数（`INTEGER`、`VARCHAR`、`TEXT`、`TIMESTAMP` 等）。
- ✅ 通过 `Base.metadata.create_all` 进行表创建（已修复 `pg_type` 反射兼容问题）。
- ✅ `SELECT ... FOR UPDATE` 行级锁。

### 4.2 不支持 / 关闭的功能

- ❌ GaussDB 特有的存储过程、特有数据类型扩展、专有索引策略。
- ❌ PostgreSQL native ENUM / UUID（已由方言关闭：`supports_native_enum=False`、`supports_native_uuid=False`）。
- ❌ `INSERT ... VALUES` 多行批量优化路径（`use_insertmanyvalues=False`）。
- ❌ `asyncpg` 自定义类型编解码器、`LISTEN/NOTIFY`、PostgreSQL 复制/通知机制。
- ❌ 服务端游标的高级用法、PG 特定的复杂锁子句（`OF`、`SKIP LOCKED` 等）。

### 4.3 兼容性建议

- 建议使用标准 SQL 函数（如 `func.current_timestamp()`）替代 PG 专有函数（如 `func.now()`）。
- 字符串列优先使用 `String(N)` 并显式给定长度；`GaussString` 会兜底将 `datetime`/数字等类型转换为字符串。
- 长度大于一定值的字段建议使用 `Text` 而非超长 `String(N)`。
- DDL 操作建议在应用启动一次性完成，避免运行时反射触发的兼容性问题。

### 4.4 运行时依赖

| 依赖包 | 版本要求 | 说明 |
|--------|----------|------|
| `sqlalchemy` | >= 2.0.41 | 主依赖 |
| `async-gaussdb` | ~= 0.30.0 | GaussDB 异步驱动，可选依赖 |
| `asyncpg` | >= 0.29.0 | SQLAlchemy `PGDialect_asyncpg` 在错误翻译（`_asyncpg_error_translate`）和异步连接适配中需引用 `asyncpg.exceptions`，必须存在 |

`asyncpg` 必备的原因：`GaussDialectAsyncpg` 继承自 `PGDialect_asyncpg`，复用了其 `AsyncAdapt_asyncpg_dbapi` 适配层；运行时一旦发生数据库异常，`_handle_exception` 会执行 `import asyncpg`，缺失会引发 `ImportError`。

---

## 5. 与长期记忆引擎集成

`GaussDbStore` 实现 `BaseDbStore` 抽象，可作为 `LongTermMemory.register_store(...)` 的 `db_store` 参数使用，替代默认的 `DefaultDbStore`。在 `register_store` 内部，记忆引擎会调用 `create_tables(db_store)` 创建以下三张持久化表：

| 表名 | 模型类 | 用途 |
|------|--------|------|
| `user_message` | `memory_core.manage.mem_model.db_model.UserMessage` | 多轮对话原始消息（按 `user_id` / `scope_id` / `session_id` 关联） |
| `scope_user_mapping` | `ScopeUserMapping` | scope 与 user 的多对多映射关系，用于按 scope 批量清理记忆 |
| `memory_meta` | `MemoryMeta` | 各业务表的 schema 版本号，配合 `run_sql_migrations` 做迁移 |

表结构概览：

```python
# user_message
message_id  VARCHAR(64)  PRIMARY KEY
user_id     VARCHAR(64)  NOT NULL
scope_id    VARCHAR(64)  NOT NULL
content     VARCHAR(4096) NOT NULL
session_id  VARCHAR(64)
role        VARCHAR(32)
timestamp   VARCHAR(32)

# scope_user_mapping
user_id     VARCHAR(64)  PRIMARY KEY
scope_id    VARCHAR(64)  PRIMARY KEY

# memory_meta
table_name      VARCHAR(64)  PRIMARY KEY
schema_version  VARCHAR(64)  NOT NULL
```

### 5.1 注册到 `LongTermMemory`

```python
from sqlalchemy.ext.asyncio import create_async_engine
from foundation.store.db.gauss_db_store import GaussDbStore
from memory_core.long_term_memory import LongTermMemory

engine = create_async_engine(
    "gaussdb+async_gaussdb://user:password@host:port/agentmgr"
)
db_store = GaussDbStore(async_conn=engine)

memory = LongTermMemory()
await memory.register_store(
    kv_store=kv_store,            # 实现 BaseKVStore
    vector_store=vector_store,    # 可选，实现 BaseVectorStore
    db_store=db_store,            # 此处接入 GaussDB
)
# register_store 内部会自动调用 create_tables(db_store) 建表，
# 并通过 run_sql_migrations 维护 memory_meta 中的 schema_version。
```

### 5.2 直接使用 `GaussDbStore` 操作长期记忆表（建表 + 增删改查）

下例完整演示通过 GaussDB 引擎对 `user_message` 表执行 DDL 与 CRUD。`scope_user_mapping`、`memory_meta` 操作模式相同。

```python
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from foundation.store.db.gauss_db_store import GaussDbStore
from memory_core.manage.mem_model.db_model import (
    Base,
    UserMessage,
    ScopeUserMapping,
    MemoryMeta,
    create_tables,
)


GAUSSDB_URL = "gaussdb+async_gaussdb://user:password@host:port/agentmgr"


async def main():
    # 1. 构造 GaussDbStore
    engine = create_async_engine(GAUSSDB_URL, echo=False)
    store = GaussDbStore(async_conn=engine)
    engine = store.get_async_engine()

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 2. 建表（与 LongTermMemory.register_store 内部行为一致）
    #    方式 A：直接调用 db_model.create_tables，会创建
    #    user_message / scope_user_mapping / memory_meta 三张表，
    #    并自动处理旧版本字段迁移。
    await create_tables(store)

    #    方式 B：仅创建 Base 下的所有表（不包含旧版本字段处理）
    # async with engine.begin() as conn:
    #     await conn.run_sync(Base.metadata.create_all)

    # 3. 插入一条用户消息
    async with async_session() as session:
        msg = UserMessage(
            message_id="msg-0001",
            user_id="user-001",
            scope_id="scope-default",
            session_id="session-abc",
            role="user",
            content="你好，帮我记住我喜欢喝美式咖啡。",
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        session.add(msg)

        # 同步记录 scope-user 映射（去重逻辑由业务层保证或使用 INSERT ... ON CONFLICT）
        mapping = ScopeUserMapping(user_id="user-001", scope_id="scope-default")
        session.add(mapping)

        await session.commit()

    # 4. 查询：按 user_id + scope_id 检索消息
    async with async_session() as session:
        stmt = (
            select(UserMessage)
            .where(UserMessage.user_id == "user-001")
            .where(UserMessage.scope_id == "scope-default")
            .order_by(UserMessage.timestamp.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        for row in result.scalars():
            print(f"[{row.timestamp}] {row.role}: {row.content}")

    # 5. 更新：修正某条消息内容
    async with async_session() as session:
        stmt = (
            update(UserMessage)
            .where(UserMessage.message_id == "msg-0001")
            .values(content="你好，请记住我喜欢喝美式咖啡（已确认）。")
        )
        await session.execute(stmt)
        await session.commit()

    # 6. 删除：按 scope 清空消息
    async with async_session() as session:
        stmt = delete(UserMessage).where(UserMessage.scope_id == "scope-default")
        await session.execute(stmt)
        # 同步清理 scope-user 映射
        stmt = delete(ScopeUserMapping).where(
            ScopeUserMapping.scope_id == "scope-default"
        )
        await session.execute(stmt)
        await session.commit()

    # 7. 查询 memory_meta 中各表 schema 版本（迁移用元数据）
    async with async_session() as session:
        stmt = select(MemoryMeta)
        result = await session.execute(stmt)
        for row in result.scalars():
            print(f"table={row.table_name}, schema_version={row.schema_version}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

### 5.3 自定义业务表的建表与 CRUD

如果业务侧需要在 GaussDB 上扩展自有表，建议复用现有 `Base` 或新建 `DeclarativeBase`，其余流程与上面完全一致：

```python
from sqlalchemy import String, Integer, Float, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(String(50))
    stock: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# 建表
async with store.get_async_engine().begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
```

后续 `INSERT / SELECT / UPDATE / DELETE` 全部使用 SQLAlchemy 标准 API，不再赘述。

---

## 6. 最佳实践

1. **使用标准 SQLAlchemy 接口**

   ```python
   async with async_session() as session:
       session.add(UserMessage(...))
       await session.commit()
   ```

2. **避免使用数据库特定语法**，优先使用 `func.current_timestamp()` 等标准 SQL 函数。

3. **处理可选依赖**

   ```python
   try:
       from foundation.store.db.gauss_db_store import GaussDbStore
   except ImportError:
       from foundation.store.db.default_db_store import DefaultDbStore as GaussDbStore
   ```

4. **DDL 在启动期统一执行**：通过 `LongTermMemory.register_store` 自动建表，或显式调用 `create_tables(store)`，避免运行时反射造成额外开销与兼容性风险。

5. **复用 `engine`**：`GaussDbStore` 多次调用 `get_async_engine()` 返回同一实例，业务侧应共享一个 `AsyncEngine` 与 `sessionmaker`，并在进程退出前 `await engine.dispose()`。

---

## 7. 安装方式

```bash
# 单独安装驱动（最直接）
pip install async-gaussdb asyncpg sqlalchemy>=2.0.41
```

如项目通过 extras 管理可选依赖，可使用对应 extras 安装（例如 `pip install <package>[gaussdb]`），实际命名以项目 `pyproject.toml` 为准。
