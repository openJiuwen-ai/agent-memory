# F04 — 存储后端 SSL 配置

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-31 |
| 影响范围 | `jiuwen_memory/common/_support.py`（共用件）、`jiuwen_memory/storage/_support.py`、redis / elasticsearch / milvus / postgres / pgvector 五个 builder、`deploy/docker/online/`、`deploy/docker/postgres/` |
| 测试基线 | `tests/unit/storage` 118 passed, 2 skipped |
| Refs | [S06-storage.md](../../specs/S06-storage.md) 不变量 17、[F05-model-service-ssl.md](../common/F05-model-service-ssl.md) |

## 背景

存储后端需对接云端托管实例（DCS Redis、CSS Elasticsearch、RDS for PostgreSQL 等），
链路须加密并校验服务端身份。云厂商普遍使用**私有 CA 自签**服务端证书，客户端必须
显式指定 CA 证书路径——否则客户端拿系统 CA 校验必然失败。

改动前各 builder 只把连接串交给客户端，其余构造参数一律不透传，证书路径无处可传。

## 决策

### 一、两个配置参数，各后端自行翻译

```yaml
params:
  ssl_verify: "${X_SSL_VERIFY:-false}"   # 是否校验服务端证书
  ssl_ca_cert: "${X_SSL_CA:-}"           # CA 证书路径
```

读取逻辑收在 `storage/_support.read_ssl_config`，**翻译留在各 builder**：

| 后端 | `ssl_verify=true` → | `ssl_ca_cert` → |
|---|---|---|
| redis | 校验 url 为 `rediss://` | `ssl_ca_certs` |
| elasticsearch | 校验 hosts 为 `https://` | `ca_certs` |
| postgres / pgvector | `sslmode="verify-full"` | `sslrootcert` |
| milvus | `secure=True` | `server_pem_path` |

不做跨后端的参数抽象层：四个客户端的参数名、类型与语义切分互不相同（"是否校验"
在 redis 是 `ssl_cert_reqs` 枚举 + `ssl_check_hostname` 两维、在 pg 是 `sslmode`
六档枚举、在 ES 是三个独立布尔），统一抽象只能取交集或退化为并集，收益抵不过映射成本。

### 二、`ssl_verify` 只管校验，不管加密

加密开关落在连接串上，四个后端形态不同：redis/ES 只认 scheme（redis-py 的
`ssl=True` 实测不生效，elasticsearch-py 8.x 已移除 `use_ssl`），pg 靠 `sslmode`，
milvus 两者皆可。因此本参数**不承诺开启加密**。

redis 与 elasticsearch 在开启时额外校验 scheme：若声明校验而连接串仍为明文，
证书参数会被传入却不生效，连接以明文建立而调用方以为已加密——静默失败比报错危险，
故拦在装配期。elasticsearch 的 `hosts` 支持多节点列表，须逐个校验。

### 三、开启但缺证书即报错，不回退系统 CA

云厂商自签场景下回退系统 CA 必然校验失败，而那个报错指向证书链、看不出是配置漏项。
在装配阶段拦截并直接点名缺失的参数。

### 四、开启时拒绝连接串自带 `ssl_*` 参数（redis）

redis-py 的 `from_url` 让 URL query **覆盖** kwargs（实测），故
`rediss://…?ssl_cert_reqs=none` 会静默关闭校验，而配置仍声称 `ssl_verify=true`。
显式回传 `ssl_cert_reqs="required"` 压不住（同样被覆盖），只能拒绝。

其余三个后端无此风险：pg 是 kwargs 覆盖 DSN（方向相反），ES 与 milvus 的连接串
不承载 TLS 参数。

### 五、默认关闭

现有部署全部是容器内明文互联，默认开启会让它们在装配阶段直接报错。
`ssl_verify=false` 时不组装任何 TLS 参数、不做任何校验，行为与本特性引入前一致，
同时保留"把 TLS 完全交给连接串自理"的逃生舱。

### 六、公共件下沉到 common，storage 只保留自有策略

出站模型服务（见 [F05](../common/F05-model-service-ssl.md)）需要同一套归一与校验逻辑，
故公共部分下沉，避免两处各写一份：

| 住在 `common/_support.py` | 住在 `storage/_support.py` |
|---|---|
| `as_bool`、`SslConfig`、`build_ssl_config` | `read_ssl_config`（含缺证书即报错这条自有策略） |
| `require_tls_scheme`（支持地址列表与 `allow_empty`） | `reject_url_tls_params`（redis-py 特有的 URL query 覆盖问题） |

`reject_url_tls_params` 不下沉：它针对的是 redis-py「URL query 覆盖 kwargs」这一独有
行为，其余客户端不存在该风险。

## 拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| 统一的 `ssl_enabled` 开关 | redis/ES 的加密开关只存在于 scheme，参数形态无法开启；要生效需改写连接串字符串，引入两个真相来源 |
| 统一 TLS 参数抽象层（`tls_verify_cert` / `tls_verify_hostname` …） | 四个客户端语义切分不同，抽象会丢失表达能力或退化成并集；每新增一项能力需改四处映射 |
| 构造统一的 `ssl.SSLContext` | 只有 elasticsearch 接受；redis 用自有参数、pg 走 libpq、milvus 走 gRPC 自带 BoringSSL |
| 证书路径白名单（`SAFE_CERT_DIR`） | 配置由运维掌握，非外部输入；当前阶段不引入 |

## 验证

- `tests/unit/storage/test_ssl_config.py` 36 项：布尔归一、缺证书报错、scheme 校验
  （含多节点列表）、URL TLS 参数拒绝、四个后端在客户端构造边界的最终参数断言。
- 客户端参数经注入假客户端观察，不触碰被测对象受保护成员。
- redis 侧另经真实 `redis-py` 确认 `SSLConnection` 实例化后
  `cert_reqs=CERT_REQUIRED`、`ca_certs` 到位。

## 已知遗留

- **未覆盖 mTLS**：客户端证书与私钥（`ssl_certfile` / `client_cert` / `sslcert` /
  `client_pem_path`）未纳入。DCS 明确不支持双向认证，CSS 仅独享型 ELB 场景需要。
  需要时按同样模式增补两个参数。
- **主机名校验不可单独关闭**：`verify-full` 与客户端默认均校验主机名，用 IP 直连
  且证书 CN 为域名时会失败。当前须改用域名，或置 `ssl_verify=false` 由连接串自理。
- **milvus 参数依据为官方文档与源码，未经真实实例验证**：`server_pem_path` 用于
  单向认证、`ca_pem_path` 属双向分支（须与客户端证书、私钥三者同时提供才生效）。
