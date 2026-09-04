# F05 — 模型服务 SSL 配置

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-01 |
| 影响范围 | `jiuwen_memory/common/_support.py`（新增，含从 storage 与 security 下沉的公共件）、LLM（openai / dashscope）、embedder（openai）、reranker（api）四个 builder、`deploy/docker/online/`、`deploy/docker/postgres/` |
| 测试基线 | `tests/unit/common/test_outbound_ssl.py` 44 passed；`pytest -m unit` 542 passed；全量 `tests/unit` 925 passed、2 failed、2 skipped（失败因环境缺少 `torch`，与本特性无关） |
| Refs | [S07-common.md](../../specs/S07-common.md)、[F04-storage-ssl.md](../storage/F04-storage-ssl.md) |

## 背景

出站模型服务（LLM / Embedder / Reranker）需同时支持两种部署：

- **公网端点**（DashScope 等）：证书由公共 CA 签发，系统 CA 即可校验
- **私有部署**：自签证书，系统 CA 校验必然失败，须显式给出 CA 路径

改动前三者只把 `base_url` 和 `api_key` 交给客户端，证书路径无处可传——私有部署一旦
启用 HTTPS 自签就无法接入。

同时开发侧有相反诉求：本地自测常用 `http://` 直连模型服务，快速验证链路，此时不应
被任何 SSL 要求阻塞。

## 决策

### 一、两个参数，语义与 storage 对齐

全局命名空间下按组件前缀区分：

```yaml
globals:
  llm_ssl_verify: "${LLM_SSL_VERIFY:-false}"
  llm_ssl_ca_cert: "${LLM_SSL_CA:-}"
  embedder_ssl_verify / embedder_ssl_ca_cert
  reranker_ssl_verify / reranker_ssl_ca_cert
```

分三组而非共用一组：三者可能指向不同端点、不同证书。配置里可都引用同一环境变量。

### 二、`ssl_verify=false` 是「不干预」，不是「跳过验证」

关闭时不向客户端传任何 TLS 参数：`http://` 明文直连（开发自测），`https://` 仍走
SDK/httpx 默认的公共 CA 校验（实测 openai SDK 默认 `verify_mode=CERT_REQUIRED`、
`check_hostname=True`）。

若把 `false` 定义为「跳过验证」，默认值就会把现在正常校验的公网连接悄悄改成不校验，
是安全倒退。因此本特性**不提供**关闭校验的开关——拿不到证书时应改用 `http://`
（开发）或补齐证书（生产）。

### 三、`ssl_verify=true` 时强制 https

加密开关只存在于 `base_url` 的 scheme。若放行 `http://`，证书参数会被传入却不生效，
请求以明文发出而调用方以为已加密。生产配置误写成 `http://` 是本校验的主要拦截目标。

`base_url` 为空时放行——SDK 会回落到内置官方端点，均为 https。

### 四、缺证书时放行，回落系统 CA

这是与 storage 侧的**唯一矩阵差异**：

| `ssl_verify=true` 且无证书 | storage | 模型服务 |
|---|---|---|
| 行为 | **装配报错** | **放行**，用系统 CA |
| 依据 | 云厂商托管实例一律私有 CA，缺证书必然连不上 | 公网端点走公共 CA，缺证书是正常状态 |

统一表述是「`ssl_verify=true` 要求存在有效信任锚」，只是系统 CA 对两类场景的有效性
不同，而代码无法判断，只能按场景先验写死。

完整矩阵对照见下节。

### 五、证书文件缺失在装配期拦截

httpx 构造客户端时立即加载证书，路径写错只抛不带上下文的 `FileNotFoundError`。
容器内路径与宿主机路径写混是常见配置错误，故先行校验并指明组件与参数名。

### 六、仅在需要时注入 `http_client`

openai SDK 自建的客户端带长读取超时、大连接池与自动重定向等默认参数，裸
`httpx.Client` 的默认值并不等价。因此只有 `ssl_verify=true` 时才显式传入
`http_client`，且必须使用 `openai.DefaultHttpxClient(verify=…)`：既注入指定信任锚，
又保留 SDK 的默认网络参数。

`verify` 的取值统一经 `outbound_verify` 翻译（有证书用路径、无证书用 `True`），
不在各实现里内联——三处内联曾是本特性初版的重复点。

### 七、与 storage 共用同一套公共件

两侧的归一与校验逻辑一致，故公共部分住在 `common/_support.py`，各自只保留自有策略：

| 共用（`common/_support.py`） | 出站客户端自有 | storage 自有 |
|---|---|---|
| `as_bool`、`SslConfig`、`build_ssl_config` | `read_outbound_ssl`（按 `<prefix>_` 前缀读） | `read_ssl_config`（缺证书即报错） |
| `require_tls_scheme`、`require_ca_file`、`outbound_verify` | `require_https`（薄封装，`allow_empty=True`） | `reject_url_tls_params`（redis-py 特有） |

`require_tls_scheme` 的 `allow_empty` 参数即为本特性引入：`base_url` 未配置时要回落
SDK 内置端点，而 storage 侧不允许空连接串。

`common/_support.as_bool` 同时收编了 `common/security` 早先的私有副本，全仓归一逻辑
只此一份。

## 两侧矩阵对照

| # | `ssl_verify` | 连接串 | 证书 | storage | 模型服务 |
|---|---|---|---|---|---|
| 1 | false | 明文 | — | 放行，明文 | 放行，明文 |
| 2 | false | TLS | — | 放行，客户端默认校验（系统 CA） | 放行，SDK 默认校验（系统 CA） |
| 3 | true | 明文 | — | **报错** | **报错** |
| 4 | true | TLS | 无 | **报错** | **放行**，系统 CA |
| 5 | true | TLS | 有 | 指定 CA | 指定 CA |

五格中四格一致，仅第 4 格按场景先验分化。

## 拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| 只加 `ssl_ca_cert`、不加开关 | 无法表达「开发用 http 自测」与「生产强制 SSL」的意图差异，也失去第 3 格的配错拦截 |
| `ssl_verify` 默认 true | 现有 `.env` 未声明该参数，用 `http://` 自部署模型服务的部署会突然装配失败 |
| `false` 表示跳过校验 | 默认值会把现在校验着的公网连接改成不校验，安全倒退 |
| 三者共用一组参数 | 可能指向不同端点与证书；共用后无法分别配置 |

## 验证

- `tests/unit/common/test_outbound_ssl.py` 44 项：布尔归一、前缀隔离、空串归一、
  verify 取值翻译、https 强制、证书缺失拦截，以及四个组件 × 四种装配状态。
- reranker 侧经注入假 httpx.Client 断言 `verify` 的最终取值，并验证关闭时不传该参数。
- OpenAI LLM / Embedder 经替身工厂断言使用 `openai.DefaultHttpxClient`，自定义 CA 不再
  把 SDK 的网络默认值退化为裸 httpx 默认值；测试类方法均满足 G.CLS.07。

## 已知遗留

- **未覆盖 mTLS**：私有部署若要求客户端证书，需另加 `cert=` 参数（httpx 支持）。
- **无法跳过校验**：自签且暂时拿不到证书时只能改用 `http://`。这是有意取舍，见「拒绝的方案」。
- **embedder 在装配期即构造客户端**：与 LLM 的惰性构造不一致（缺 `api_key` 时装配即
  失败）。属既有行为，本次未改动。
