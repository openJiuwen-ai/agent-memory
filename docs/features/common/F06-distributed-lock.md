# F06 — 分布式锁接口与 Redis 实现

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-03 |
| 影响范围 | `jiuwen_memory/common/lock/`（新增）、`jiuwen_memory/common/_support.py`（接收从 storage 下沉的公共件）、`jiuwen_memory/common/bootstrap.py`、`jiuwen_memory/common/AGENTS.md`、`jiuwen_memory/storage/_support.py`（改为再导出）、`examples/config_template.yml`、`docs/specs/S07-common.md` |
| 测试基线 | `tests/unit/common/test_distributed_lock.py` 37 passed；独立 `redis:7-alpine` 容器黑盒验证通过（跨实例互斥、TTL 后旧 token 不释放新锁、自动续期、失锁通知、PING），未新增真实 Redis 集成测试；全量 `tests/unit` 862 passed、9 failed、4 skipped（失败与跳过均因环境缺少 `cryptography` / `torch` / `psycopg_pool`，改动前后一致，与本特性无关）；`ruff check` 对本次改动文件全部通过 |
| Refs | [S07-common.md](../../specs/S07-common.md)、[F04-security-interfaces-and-encryption.md](F04-security-interfaces-and-encryption.md)、[F05-model-service-ssl.md](F05-model-service-ssl.md) |

## 背景

记忆服务以多实例部署，同一用户的请求可落到任意实例。当前代码库不存在任何跨实例互斥
原语，需要串行化的场景只能各自约定，或者干脆不做。

本特性**只交付原语本身**：一个可插拔的分布式锁接口与其 Redis 实现。不改动任何业务
路径，不在 `CloudEngine`、`Scheduler`、`LifecycleManager` 等处插入加锁点。具体在哪些
临界区加锁、锁多大范围，由后续各自的特性单独论证与归档。

## 决策

### 一、落位：`common/` 下的横切组件

`jiuwen_memory/common/AGENTS.md` 铁律 7 已确立横切组件的形态——`SecurityProvider` 与
`AuditLogger` 不继承 `Plugin`、不进入 `PluginType`，但仍用独立 Producer 加 `*_impl`
自注册。锁是第三个同类组件，完全复用该形态。

```
jiuwen_memory/common/lock/
├── __init__.py                  再导出公开符号
├── lock.py                      LockProvider 契约 + LockProducer + 错误类型
└── lock_impl/
    ├── __init__.py              import 各实现触发注册（Redis 客户端在首次建连时延迟导入）
    ├── redis_lock.py            @LockProducer.register("redis")
    └── in_memory_lock.py        @LockProducer.register("memory")
```

`jiuwen_memory/common/bootstrap.py::register_plugins()` 追加
`import_module("jiuwen_memory.common.lock.lock_impl")`。
`LockProducer.TOP_NAME = "lock"` 一经导入即成为配置的合法顶层段——`jiuwen_memory/config/context.py`
的顶层段校验取自 `Factory.known_top_names()`，无需额外登记。

不落在 `storage/`：锁不是记忆数据的读写通道，不参与 `BaseStore` 的 CRUD 动词契约，
也不应被 `EncryptedKVStore` 之类的装饰器链路径过。

### 二、契约异步

`common/` 现有代码全同步（无 `async def` / `await` / `asyncio` 引用），本组件是第一处
异步。三条理由：

1. 预期消费方（`CloudEngine` 及其下游编排）本身是协程；
2. 租约续期需要后台执行体，异步下是一次 `create_task`，同步下要为每个持有中的锁起守护
   线程；
3. 有界等待在同步实现中会占住调用线程，异步下 `await asyncio.sleep` 不占用执行资源。

`health()` 随之也是 `async def`，与其余组件的同步 `health()` 不一致。现有 `health()`
调用点全部是组件间的同步级联（`encrypted_kv_store.py:79`、`metadata_pipeline.py:51` 等），
无全局聚合器遍历，故当前不产生破坏；未来若有消费方级联调用，须自行 `await`。

### 三、接口

```python
class LockProducer(Factory):
    TOP_NAME = "lock"


class LockError(AgentMemoryError): ...
class LockTimeoutError(LockError): ...   # 有界等待超时未获得
class LockLostError(LockError): ...      # 租约续期失败，持有权已失效


@dataclass
class LockHandle:
    key: str          # 完整锁键（含前缀）
    token: str        # 本次持有的唯一标识，释放与续期的 CAS 依据
    lease_ms: int
    reentrant: bool   # True 表示本次是重入，未真正向后端申请
    lost: asyncio.Event   # 续期失败时置位


class LockProvider(ABC):
    async def acquire(self, scope: Scope, name: str, *,
                      lease_ms: int | None = None,
                      wait_timeout_ms: int | None = None) -> LockHandle: ...

    async def release(self, handle: LockHandle) -> None: ...

    @abstractmethod
    async def renew(self, handle: LockHandle, *, lease_ms: int | None = None) -> bool: ...

    def guard(self, scope: Scope, name: str, **kwargs) -> AbstractAsyncContextManager[LockHandle]:
        """acquire / 自动续期 / release 的组合，推荐入口。"""

    async def health(self) -> None: ...

    # 后端原语，由各实现提供
    @abstractmethod
    async def _acquire(self, key: str, *, lease_ms: int, wait_timeout_ms: int) -> LockHandle: ...
    @abstractmethod
    async def _release(self, handle: LockHandle) -> None: ...
```

`acquire` / `release` / `guard` 在 `lock.py` 中给出具体实现，只有后端相关的三个原语抽象。
这偏离铁律 1 的「接口模块零依赖实现」，理由是重入记账与 guard 组合属于**契约级行为**而
非后端细节：放到实现层会在两个实现里重复，且两份重入语义可能分叉。

### 四、锁键与粒度

```
am:lock:v1:{org}:{space}:{user}:{agent}:{session}:{name}
```

五段 scope 定长渲染，空维度以 `_` 占位，段内的 `/` 与 `:` 替换掉——与 KV/FS 命名空间
同一套规则，复用 `scope_segments`。

`am:lock:v1:` 前缀必须保留：KV 数据键是裸的五段命名空间，锁与数据共用一个 Redis 库时
会撞键。`v1` 供将来键结构变更时并存过渡。

**粒度由调用方决定，本组件不预设**。要用户级互斥就传一个 `agent` / `session` 置空的
`Scope`；要更细的区分维度就写进 `name`。这条写进 `acquire` 的 docstring——粒度是业务
判断，锁原语不该替调用方选。

### 五、Redis 实现

**获取**：`SET key token NX PX lease_ms` 单命令，原子。`token` 为 `uuid4`。

**释放**：Lua 脚本做 CAS，避免删掉他人在租约过期后重新获得的锁。

```lua
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
```

**续期**：同构 Lua，`PEXPIRE` 替换 `DEL`。返回 0 表示已失去持有权。

**竞争等待**：有界。默认 `wait_timeout_ms = 10000`，退避初值 20ms、系数 1.6、上限 200ms、
全抖动。超时抛 `LockTimeoutError`，不无限自旋。

**租约与续期**：默认 `lease_ms = 30000`，`guard` 内以 `lease_ms / 3` 为周期续期。续期返回
False 时置位 `handle.lost` 并终止续期循环。持有者可在临界区内检查 `handle.lost.is_set()`
决定放弃还是继续——续期失败意味着持有权已经丢失，若无通知，持有者会在无锁状态下把临界区
执行完。

**重入**：以 `asyncio.current_task()` 为身份边界。同一 task 内嵌套获取同一键时递增计数、
直接返回原 handle（`reentrant=True`，`guard` 据此不再起第二个续期任务）；`create_task`
派生的子任务不视为重入，会正常参与竞争。这条语义须在文档与 docstring 中写明——它决定了
消费方能否在同一调用栈内安全嵌套。

**Redis 不可用**：fail-closed，异常归一为 `BackendError` 向上抛，不提供静默降级为无锁的
旁路。与 F04-storage-ssl 的处置一致。

**客户端**：`redis.asyncio.Redis.from_url`。`redis>=5` 已在 `pyproject.toml` 的 `deploy`
extra 中声明，无新增依赖。与 redis KV 后端各自持有连接池，互不复用。

**TLS**：与 redis KV 后端同一套参数（`ssl_verify` / `ssl_ca_cert`）与同一套装配期校验
（`rediss://` scheme 强制、拒绝 URL 自带 `ssl_*` 查询参数）。

### 六、`memory` 实现

进程内字典加租约，供单测与无 Redis 的本地开发使用。**不提供跨实例互斥**，这一点在类
docstring 与配置注释中显式标注。

`LockProducer` 不设默认实现：消费方 `dep(config)` 时必须显式配置，缺配置报错。让
「忘了配 Redis」在装配期失败，而不是静默退化成单机锁。

### 七、从 storage 下沉的公共件

`jiuwen_memory/common` 不能反向依赖 `jiuwen_memory/storage`，而 Redis 实现需要的四个工具目前都在
`jiuwen_memory/storage/_support.py`。按铁律 9 已确立的做法（SSL 公共件只实现一份，storage 与
security 共同引用），把以下内容下沉到 `jiuwen_memory/common/_support.py`，`jiuwen_memory/storage/_support.py` 改为
再导出：

| 符号 | 说明 |
|---|---|
| `SCOPE_DIMS` | 原 `_DIMS`，五维元组 |
| `scope_segments` | scope 定长五段渲染 |
| `wrap_backend` | 后端异常归一为 `BackendError` |
| `read_ssl_config` | 组件 `params` 下的 `ssl_verify` / `ssl_ca_cert` 读取与校验 |
| `reject_url_tls_params` | 连接串自带 `ssl_*` 查询参数的拦截 |

五者均为纯函数，不依赖 `jiuwen_memory/storage` 任何模块，行为无变更。`scope_dims` 留在
`jiuwen_memory/storage`——它是
检索型后端的过滤构造，与命名空间渲染是两回事。`jiuwen_memory/storage/_support.py` 已有向后兼容再导出
块，追加即可，现有 import 路径不变。

### 八、配置

```yaml
lock:
  default:
    target: redis
    params:
      url: "redis://localhost:6379/1"
      lease_ms: 30000
      wait_timeout_ms: 10000
      ssl_verify: "${REDIS_SSL_VERIFY:-false}"
      ssl_ca_cert: "${REDIS_SSL_CA:-}"
```

默认库号与 KV 分开（`/1`）：键前缀已能防撞，分库只是便于运维单独观测与清理。

**只写进 `examples/config_template.yml` 的注释块，不进 `deploy/docker/*/config.yml`**。
本次改动无消费方，`Factory` 只在有人调 `LockProducer.dep(...)` / `build_named(...)` 时才
实例化，此时往部署配置里放一段永不生效的 live 配置只会误导运维。真正接入的特性负责把它
写进部署配置。

## 语义边界

必须在文档与 docstring 中写明，避免调用方按强互斥假设编码：

- 这是**基于租约的协调机制，不是共识算法**。租约到期、进程停顿超过租约、Redis 主从切换
  丢失未同步写入，都会导致短暂双持。
- 不做 Redlock。单 Redis 部署下多节点法定人数的成本收益不匹配，且其安全性本身有争议。
- 不做 fencing token。fencing 防的是被抢占的持有者继续写入造成的数据完整性破坏，需要下游
  存储配合校验单调序号；当前无消费方提出该需求，等有明确场景再加，接口留有扩展位
  （`LockHandle` 可加字段而不破坏现有签名）。
- 因此，依赖本锁的业务必须能容忍偶发的互斥失效，或自备第二道防线（幂等键、唯一约束、
  乐观并发控制）。

## 不在本次范围

- 任何业务路径的加锁点接入
- 原子性（多存储写入的事务性）——与互斥是两个独立性质，锁不提供
- 锁的可观测性指标（持有时长、等待时长、超时率）
- Redlock、fencing token
