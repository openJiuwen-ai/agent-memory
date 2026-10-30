# Security 安全功能使用指南

最近一次修订日期：2026-10-30

本文说明记忆服务的配置、调用和安全验收。存储后端的扩展开发请查阅
[S06 存储契约](../../specs/S06-storage.md) 与 [S10 安全契约](../../specs/S10-security.md)；
安全架构、实现取舍和内部检查流程见 [F04 安全设计](../../features/common/F04-security-interfaces-and-encryption.md)。
配置的通用规则见 [Config 配置指南](config.md)。

## 1. 部署时选择哪些安全配置

使用已有服务时，向管理员取得服务地址、本人凭据和允许访问的 Scope。自行部署时，
按下表启用相应配置。认证、授权、加密与审计分别解决不同问题，不会因为其中一项
启用就自动开启其他各项。

| 你要保护什么 | 配置与验收入口 |
|---|---|
| 确认调用者身份 | 第 4 节，选择一个 `authenticator`：DEV、API Key 或 Trusted |
| 限制用户能访问的数据 | 第 6 节，配置 `authorizer`，用 Alice/Bob 验证允许和拒绝 |
| 让 agent 有限期代操作 | 第 7 节，配置委托记录、凭据/session 绑定和到期时间 |
| 保护 KV 中的正文 | 第 9 节，配置 `encrypted` KV 端口和专用加密根密钥 |
| 保护 FS、ES 等其他落盘数据 | 第 10 节，启用统一开关，并落实索引等文件的部署侧加密 |
| 保存审计记录 | 第 11.1 节，将审计器指向文件型数据库 |
| 检查审计记录是否被改动 | 第 11.2 节，同时配置审计库、专用密钥和 `audit_integrity` |
| 通过 MCP 接入 | 第 12 节，选择传输方式，并为 MCP 进程提供匹配的配置和凭据 |

随附脚本和配置统一在 [examples/security](../../../examples/security/README.md)。
它们使用内存数据面、回环临时端口和系统临时文件，不需要外部模型或数据库，
也不用把示例复制到仓库根目录。Linux 加密卷的部署另按第 10.5 节验收。

## 2. 一次请求如何经过安全检查

以 Alice 读取自己的记忆为例：服务先验证她的凭据，得到真实身份；再检查她是否
有权读取请求 Scope 内的数据；通过后才返回内容。若存储已加密，服务读取时解密，
正常 API 响应仍是可读正文。审计记录这次操作。

客户端提交的 Scope 表示“想访问哪里”，不会改变“谁在调用”。Bob 将 Scope 写成
Alice 的范围，并不能获得 Alice 的身份或权限。需要排查判权原理时，查阅
[F04 的授权说明](../../features/common/F04-security-interfaces-and-encryption.md#3-授权判定与真源)。

## 3. 本地快速开始

以下命令均从仓库根目录执行。先准备 Python >=3.11 的虚拟环境。Bash：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,security]"
export PYTHONPATH="$PWD:$PWD/jiuwen_memory_entry/core"
```

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,security]"
$env:PYTHONPATH = "$($PWD.Path);$($PWD.Path)\jiuwen_memory_entry\core"
```

后文的 `python` 均指上述已安装依赖的环境。MCP 另需第 12 节的 extra。

### 3.1 HTTP 与 CLI

以下 DEV 服务仅供本机回环测试，固定身份是 `local/developer`，默认角色为 USER，
不需要 Key。Bash 启动服务：

```bash
scripts/run-server.sh examples/security/dev.yml --host 127.0.0.1 --port 8137
```

PowerShell 启动服务：

```powershell
python -m jiuwen_memory_entry.http_server examples/security/dev.yml --host 127.0.0.1 --port 8137
```

保持服务运行，另开终端，从仓库根目录发送一次请求。Bash：

```bash
curl -i -X POST http://127.0.0.1:8137/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"content":"local demo","scope":{"org":"local","user":"developer"}}'
scripts/run-cli.sh --server http://127.0.0.1:8137 add \
  --content 'local CLI demo' --scope '{"org":"local","user":"developer"}'
```

PowerShell：

```powershell
$body = @{content='local demo'; scope=@{org='local'; user='developer'}} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8137/v1/add' -Method Post `
  -ContentType 'application/json' -Body $body
```

预期 HTTP 返回 200 和记忆列表。保存列表中一条记忆的 `id` 后，可用同一 Scope
调用 `POST /v1/get`，正文形状为 `{"unit_id":"实际 ID","scope":{"org":"local","user":"developer"}}`。
HTTP 返回的记忆正文在 `segments` 中，本例为 `segments[0].content`；不是顶层 `content`。

需要跨多次请求验证数据时，使用同一个持续运行的 HTTP 服务。CLI 省略 `--server`
会另起本地 Runtime，一条命令结束后不能依赖其内存数据。测试完成后在服务终端按 Ctrl+C。

### 3.2 Python SDK

```bash
python examples/security/quickstart.py
```

预期显示 `PASS SDK quickstart: write/read/search`。此例使用 DEV USER 和标准授权，
不需要模型或数据库。SDK 示例统一通过 `assemble_server_runtime` 装配，再调用
`runtime.authenticate(Credentials(...))` 取得请求上下文。普通 API 调用方式见第 5.3 节。

## 4. 选择一种认证模式

一个服务使用 `security.default.params.authenticator` 选中的一个认证器。
DEV、API Key 和 Trusted 是替换使用的方式；Root Key 和普通用户 Key 则同属 API Key 模式。

| 模式 | 用途 | 客户端提交什么 |
|---|---|---|
| 固定 DEV | 本机调试，所有请求使用同一个测试身份 | 不需要 Key；默认 USER |
| 映射 DEV | 本机模拟 Alice、Bob 等不同用户 | 服务端预设的 selector，例如 `demo-alice` |
| API Key | 客户端直接访问服务 | 管理员交付的普通用户 Key；本地运维测试可用 Root Key |
| Trusted | 通过认证网关接入 | 客户端向网关认证，网关向后端发专用 Key 和身份头 |

本文的 `demo-alice`、`demo-bob`、`demo-agent` 都是**公开的 DEV 测试 selector**，
不是随机签发的生产 Key。生产环境使用 API Key 或已完成主体注册集成的 Trusted 服务。

### 4.1 DEV 配置与模式切换

完整配置是 [dev.yml](../../../examples/security/dev.yml)；固定 USER 的核心配置为：

```yaml
memory_api:
  security:
    default: {target: standard, params: {authenticator: local_dev, authorizer: default}}
  authenticator:
    local_dev: {target: dev, params: {role: user}}
```

切换为 API Key 时，先停止旧服务，设置第 4.2 节的 Root Key，再选择另一份完整配置：

```bash
scripts/run-server.sh examples/security/api-key.yml --host 127.0.0.1 --port 8137
```

PowerShell：

```powershell
python -m jiuwen_memory_entry.http_server examples/security/api-key.yml --host 127.0.0.1 --port 8137
```

两份文件二选一，不叠加加载。多个服务配置文件按顶层覆盖，后一个文件的整个
`memory_api` 可能替换前一个文件的配置；新增具名认证器也不会自动切换已选中的引用。

验收方式：DEV 业务请求不带认证头返回 200；API Key 模式不带认证头或带错误 Key
返回 401，正确 Root Key 返回 200。`/healthz` 只检查存活，不能用来判定切换成功。
第 4.2.1 节的脚本会自动完成这组验证。

`--auth-mode dev` 只在没有显式 `security` 时补入 DEV，不覆盖已有 API Key 配置。
默认 `required` 不会替你生成安全配置；远程 CLI 也不能用 `--auth-mode dev` 改变服务端模式。

### 4.2 API Key 与两个环境变量

完整配置是 [api-key.yml](../../../examples/security/api-key.yml)。它选择 `api_key`
认证器，引用主体 KeyStore，并从 `${AGENT_MEMORY_ROOT_API_KEY}` 读取部署级 Root Key。
另有认证限流、并发预算和回环绑定配置，参数说明见第 8 节。

| 变量 | 谁使用 | 应填什么 |
|---|---|---|
| `AGENT_MEMORY_ROOT_API_KEY` | 服务端 YAML 引用 | 部署级 Root 凭据 |
| `AGENT_MEMORY_API_KEY` | CLI、stdio MCP 等客户端读取 | 本次调用实际使用的 Key |

**本地 Root 测试中，两个变量显式赋成同一个值。** 程序不自动同步，也没有从 Root
Key 派生客户端 Key 的步骤。Root Key 认证后为 `org=system,user=root,role=ROOT`。
普通用户使用独立随机签发的 Key，不能复用 Root Key。

固定 DEV 不验证这两个变量。默认 DEV 是 USER；只有显式配置 `role: root` 的固定
DEV 与 Root Key 在角色权限上对应，身份仍为 `local/developer`，认证过程也不同。
本地 Root 功能测试需要改 DEV 角色时，修改配置后重启；设置变量或请求 JSON 中的
`role` 都不会提升 DEV 身份。

#### 4.2.1 Root 管理签发与普通用户调用

普通用户不能用用户名、Scope 或环境变量自行生成有效 Key。“Root 签发”表示：
受信管理代码先验证 Root 凭据并检查 ROOT 角色，再在**实际处理请求的同一 Runtime、
同一 KeyStore** 中调用 `issue(Scope(...), Role.USER)`，将一次返回的明文 Key
通过受保护渠道交给用户。Key 是随机生成的，不由 Root Key 计算得到。

目前没有 `/v1/issue_key`、CLI `issue_key` 或 MCP 签发工具；`issue` 本身也不检查
管理员身份，Root 检查必须由管理集成执行。普通用户不需要执行签发代码。

直接运行随附完整示例，不需要在两个终端之间复制随机密钥：

```bash
python examples/security/api_key.py
```

预期依次显示五行 `PASS`：DEV 无凭据成功；API Key 缺失/错误被拒、Root 成功；
Root 校验后签发独立 USER Key；该 Key 通过真实 HTTP 写入和读取 Alice 数据，访问
Root Scope 返回 403；最后关闭监听器。脚本自动选择回环临时端口，不与 8137 冲突。

[api_key.py](../../../examples/security/api_key.py) 把受信管理端与测试客户端放在一个进程中，
不打印密钥，不安装产品管理接口。正式部署需将签发集成进实际服务的启动或受信管理
流程。凭据的内存与跨进程限制见第 6.2.4 节。

拿到管理员交付的普通用户 Key 后，客户端显式赋值：

```bash
export AGENT_MEMORY_API_KEY='<管理员交付的普通用户 Key>'
```

```powershell
$env:AGENT_MEMORY_API_KEY = '<管理员交付的普通用户 Key>'
```

替换上述占位符，不填 Root Key。主体签发只允许 USER/ADMIN，不能 `issue(..., Role.ROOT)`。
同一主体的活跃 Key 角色不能冲突；改角色前先撤销旧 Key，再由管理方重签。

#### 4.2.2 撤销普通用户 Key

受信管理代码调用 `key_store.revoke(fingerprint(token))` 撤销主体 Key。完整验证：

```bash
python examples/security/key_revocation.py
```

预期显示 `PASS cached Key context revoked`：已认证的旧请求上下文也不能继续访问。
这是注销凭据，与第 6 节撤销一次资源分享不同。Root/网关共享 Key 的轮换由部署方
更换配置并重启相应认证器处理，不能套用普通主体 Key 的撤销流程。

### 4.3 Trusted 网关

Trusted 有两次认证：客户端先向网关证明身份；网关再向记忆后端发送网关专用 Key
和已经核实的身份头。客户端凭据与网关 Key 不相同，后者不交给普通客户端。

```text
客户端 → 客户端凭据 → 网关 → gateway_key + 身份头 → 记忆服务
```

#### 4.3.1 后端配置与公开主体初始化

完整配置见 [trusted.yml](../../../examples/security/trusted.yml)。服务选择 `target: trusted`，
再由受信启动代码登记主体；配置为：

```yaml
memory_api:
  security:
    default: {target: standard, params: {authenticator: gateway, authorizer: default}}
  authenticator:
    gateway:
      target: trusted
      params: {key_store: principals, gateway_key: "${AGENT_MEMORY_GATEWAY_KEY}"}
  key_store:
    principals: {target: memory}
  authorizer:
    default:
      target: standard
      params: {grant_store: default, delegation_store: default}
  grant_store:
    default: {target: memory}
  delegation_store:
    default: {target: memory}
```

配置负责选择认证器，主体名单由服务端受信初始化代码登记。创建 HttpServer 后、
开始接收网关请求前，通过公开 API 调用：

```python
from jiuwen_memory.api import Scope, register_trusted_principal

# service 是已按 trusted.yml 装配的 HttpServer；此处仍在受信启动流程中。
register_trusted_principal(
    service.authenticator, Scope(org="demo", user="alice"), role="user",
)
```

这个入口直接登记该服务实际使用的主体注册表，不签发普通用户 API Key，也不需要
临时切换 API Key 认证器。只能由受信部署代码调用，不是客户端注册接口，也不替代
管理调用者鉴权；普通用户无需执行它。完整可运行流程见下一节的网关示例。

登记需指定非空组织，以及 user/agent 中的一种身份；space/session 留空。角色只允许
`user/admin`，不能登记 ROOT。同主体同角色重复调用无副作用，冲突角色会被拒绝。
它是启动初始化入口，不提供动态改角色或停用功能。显式主体登记与 API Key 独立，
撤销一把 Key 不等于删除这条 Trusted 登记；存储生命周期见第 6.2.4 节。

实际接入与网关厂商无关，先约定稳定的组织、主体类型和 ID 映射，再完成以下流程：

1. 部署人员在网关和后端配置同一把专用网关 Key。
2. 服务端启动代码预先登记允许接入的主体与角色。
3. 网关用自己的登录/SSO 等方式认证客户端，再向后端发送已确认的身份头。
4. 后端验证网关 Key，查询注册角色，再检查 Scope、Grant 等业务权限；
   未注册返回 401，身份有效但无权访问返回 403。

网关与后端部署人员设置相同的 `gateway_key`，并根据已确认身份新建这些请求头：

| Header | 网关填写的值 |
|---|---|
| `Authorization: Bearer` 或 `X-API-Key` | 网关共享 Key |
| `X-Org-Id` | 组织，例如 `demo` |
| `X-Principal-Type` | `user` 或 `agent` |
| `X-Principal-Id` | 已注册主体，例如 `alice` |

网关应覆盖客户端原来的 Authorization、身份头和 `X-Role`。后端不采信客户端自述
角色；Key 不正确或主体未注册都返回 401。

#### 4.3.2 运行测试网关

```bash
python examples/security/gateway.py --self-test
```

预期显示两行 `PASS`：正确客户端凭据、缺凭据、伪造 Bob/Root 身份头、绕过网关
直连后端的结果为 **200/401/200/401**；另验证 Alice 经网关写 Bob Scope 返回 403。
当前主干的 `HttpServer.build` 已执行标准业务授权，不再是仅验证基础认证的入口。

[gateway.py](../../../examples/security/gateway.py) 加载同一份 `trusted.yml`，在服务开始
监听前调用公开初始化函数登记 Alice，再启动测试网关与后端。不导入内部认证实现，
也不使用替换安全视图的临时装配。上游客户端认证在此仅作本机模拟；真实网关可沿用
同样的后端初始化和身份头约定，替换自己的客户端认证方式。

需要手动发请求时，先设置客户端测试 Key，再启动持续监听：

```bash
export DEMO_CLIENT_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python examples/security/gateway.py
```

```powershell
$env:DEMO_CLIENT_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
python examples/security/gateway.py
```

此时网关监听 8139，后端监听 8137；先停止占用这些端口的测试服务。调用终端须使用
与启动终端相同的 `DEMO_CLIENT_KEY`；无需手工传钥的验收优先使用 `--self-test`。

```bash
curl -i -X POST http://127.0.0.1:8139/v1/add \
  -H "Authorization: Bearer ${DEMO_CLIENT_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"content":"gateway demo","scope":{"org":"demo","user":"alice"}}'
```

预期 200。附加 `X-Principal-Id: bob` 和 `X-Role: root` 后仍按 Alice 调用；缺失或
错误客户端 Key 返回 401。Ctrl+C 关闭两端监听器。脚本只代理单个接口，生产网关还
需要真实客户端认证、TLS 和后端网络隔离。默认不允许省略 `gateway_key`；明确使用
`allow_no_gateway_key=true` 的部署必须由受信反代、mTLS 或网络隔离承担边界。

## 5. 安全参数填在哪里

### 5.1 服务文件、内核配置、环境变量与请求正文

| 参数 | 谁设置 | 放在哪里 |
|---|---|---|
| 认证、授权、限流、加密与审计组件 | 部署人员 | 服务 YAML 的 `memory_api` 内 |
| Root Key、网关 Key、加密根密钥 | 部署人员 | 环境变量或私密文件，由配置引用；用途分别管理 |
| 本人的调用凭据 | 调用者 | HTTP Bearer；CLI/stdio MCP 的 `AGENT_MEMORY_API_KEY` |
| 要访问的数据范围 | 调用者 | `scope` 等业务参数，不是身份声明 |
| 请求上下文 `security=` | SDK 认证边界 | `runtime.authenticate(...)` 返回的对象；不序列化到请求 JSON |

HTTP、CLI 和 MCP 服务文件的外层可含 profile 等入口设置；内核组件配置放在
`memory_api` 内。SDK 的 `assemble_server_runtime(config=...)` 接收的是该字段的
**内层 mapping**。例如随附服务文件的 `memory_api.security`，在 SDK dict 中写作 `security`。

入口加载 YAML/JSON 时会展开 `${VAR}` 和 `${VAR:-默认值}`，CLI 的 `--config` 文件
也使用同一加载器。SDK 普通 dict 或 `yaml.safe_load` 不自动展开，须显式从环境取值
或使用已展开的配置。不要把字面字符串 `${...}` 当作实际密钥。

`target` 是组件实现名，`issuer`、`principals` 等是这份配置自己命名的实例；定义后
还要通过引用选中。修改当前完整配置后重启，避免用第二份外层文件意外覆盖原配置。

### 5.2 从 API Key 配置到一次请求

以下为本机 Root 运维测试。先设置 Root Key 并启动第 4.2 节已经给出的配置。Bash：

```bash
export AGENT_MEMORY_ROOT_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
scripts/run-server.sh examples/security/api-key.yml --host 127.0.0.1 --port 8137
```

PowerShell：

```powershell
$env:AGENT_MEMORY_ROOT_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m jiuwen_memory_entry.http_server examples/security/api-key.yml --host 127.0.0.1 --port 8137
```

另开终端调用时，新终端不会自动继承刚设置的变量；通过受保护渠道设置与服务相同
的测试 Root Key，再显式赋给客户端变量。不要在新终端另生成不同的 Root Key。

```bash
export AGENT_MEMORY_API_KEY="$AGENT_MEMORY_ROOT_API_KEY"
curl -i -X POST http://127.0.0.1:8137/v1/add \
  -H "Authorization: Bearer ${AGENT_MEMORY_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Root parameter demo","scope":{"org":"system","user":"root"}}'
scripts/run-cli.sh --server http://127.0.0.1:8137 add \
  --content 'Root CLI demo' --scope '{"org":"system","user":"root"}'
```

```powershell
$env:AGENT_MEMORY_API_KEY = $env:AGENT_MEMORY_ROOT_API_KEY
$body = @{content='Root parameter demo'; scope=@{org='system'; user='root'}} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8137/v1/add' -Method Post `
  -Headers @{Authorization="Bearer $env:AGENT_MEMORY_API_KEY"} `
  -ContentType 'application/json' -Body $body
```

预期 200；删除认证头或换错误 Key 返回 401。测试普通用户隔离时使用各人的 USER
Key，不使用 Root 替代。HTTP 服务配置是位置参数；本地 CLI 配置才用 `--config`，
远程 CLI 不再加载本地服务配置。CLI 没有通用的 `--security` 或 `--api-key` 参数。

### 5.3 SDK 请求上下文

以下是已有 Runtime 后的核心调用，完整例子见 [quickstart.py](../../../examples/security/quickstart.py)：

```python
# runtime 是已经装配的服务 Runtime，token 是该服务认可的用户 Key。
security = runtime.authenticate(Credentials(api_key=token))
target = Scope(org="demo", user="alice")
unit = runtime.api.add("private demo", target, security=security)[0]
```

`security` 表示真实调用者，`target` 表示数据归属。HTTP/CLI/MCP 替客户端完成认证
和上下文构造，不接受业务 JSON 中的 `actor`、`role` 或 Python `security` 对象。

## 6. 授权和隔离

### 6.1 如何确认授权已启用

服务配置选择 `authorizer.default`，并引用本服务实际使用的 Grant/Delegation Store。
同一组织内，普通用户默认可以操作本人范围；访问其他用户数据需独立授权。非 ROOT
不能跨组织，ADMIN 的组织管理能力也不等于自动读取所有用户正文。

使用第 6.2 节 Alice/Bob 测试验证拒绝结果，比仅看到认证成功更能说明配置是否正确。
正文不展开内部判权顺序、上下文来源证明或共享对象管理；相关契约与设计见
[S10](../../specs/S10-security.md) 和 [F04](../../features/common/F04-security-interfaces-and-encryption.md)。

### 6.2 Grant：分享自己的资源访问权

Alice 希望 Bob 阅读自己的私有记忆，使用 Grant。Bob 始终用自己的凭据调用；给 Bob
签发 Key 只解决“Bob 是谁”，不等于已经开放 Alice 的资源。Grant 也可以授给 agent。

#### 6.2.1 谁能调用授权与撤销接口

| 操作 | 认证与权限要求 |
|---|---|
| `POST /v1/grant` | 实际授予方的 Key，并对 `grantor` 范围有 `share` 权限 |
| `POST /v1/revoke` | 调用者对保存记录中的真实授予范围有 `revoke_share` 权限 |
| Bob 读取分享内容 | Bob 自己的 Key，以及同时覆盖目标范围、Bob、`read` 和有效期的 Grant |

本人拥有的私有范围通常满足分享权限，不要求 Root。只获 `read` 的 Bob 不能据此
继续分享或撤销 Alice 的授权。请求体里的 `grantor` 是开放的**资源范围**，不是
身份声明；Bob 填 `grantor=alice` 不会变成 Alice。

#### 6.2.2 HTTP 完整验收

```bash
python examples/security/grant_http.py
```

脚本自动启动 [grant-dev.yml](../../../examples/security/grant-dev.yml) 隔离服务，写入
Alice 数据并保存记忆 ID、授权 ID，再用 Bob 对同一 ID 和 Scope 读取，验证
**403 → 200 → 403**。同时检查冒充授予方、只读分享下删除、Bob 撤销 Alice 授权都
返回 403；没有凭据返回 401；撤销不删除记忆，也不注销 Bob 的 Key。预期三行 `PASS`。

需要手动测试时，选择这份配置启动服务，不与 API Key 文件叠加。Bash：

```bash
scripts/run-server.sh examples/security/grant-dev.yml --host 127.0.0.1 --port 8137
```

PowerShell：

```powershell
python -m jiuwen_memory_entry.http_server examples/security/grant-dev.yml --host 127.0.0.1 --port 8137
```

然后在另一个终端执行 `python examples/security/grant_http.py --server http://127.0.0.1:8137`，
或自行发出以下请求。已有 API Key 服务时，将 Alice/Bob selector 换成各人真实的 Key。

```bash
curl -i -X POST http://127.0.0.1:8137/v1/grant \
  -H 'Authorization: Bearer demo-alice' -H 'Content-Type: application/json' \
  -d '{"grant":{"grantor":{"org":"demo","user":"alice"},"grantee":{"org":"demo","user":"bob"},"actions":["read"]}}'
```

返回 200，正文直接是带 `grant_id` 的 Grant 对象，没有 `ok` 包装。该分享覆盖 Alice
的 Scope，不是单条记忆。`actions` 只含 `read`，不自动包含写、删或再分享；可选
`expires_at` 使用带时区的未来 ISO-8601 时间，省略/null 表示不设到期时间。

撤销时由 Alice 发送保存的 Grant 对象，必须含真实 `grant_id`。PowerShell 可直接
保存并回传响应，不必手抄 ID：

```powershell
$headers = @{Authorization='Bearer demo-alice'}
$body = @{grant=@{
  grantor=@{org='demo'; user='alice'}
  grantee=@{org='demo'; user='bob'}
  actions=@('read')
}} | ConvertTo-Json -Depth 10
$created = Invoke-RestMethod -Uri 'http://127.0.0.1:8137/v1/grant' -Method Post `
  -Headers $headers -ContentType 'application/json' -Body $body
$revokeBody = @{grant=$created} | ConvertTo-Json -Depth 10
Invoke-RestMethod -Uri 'http://127.0.0.1:8137/v1/revoke' -Method Post `
  -Headers $headers -ContentType 'application/json' -Body $revokeBody
```

成功撤销返回 200 和 JSON `null`；缺 `grant_id` 返回 400。重复撤销成功不代表原来
一定存在该 ID。若 Bob 另有有效授权，撤销一条 Grant 不会清除其他授权。

远程 CLI 的 `--grant` 直接接收 Grant JSON，不在外面再包 `grant`：

```bash
AGENT_MEMORY_API_KEY=demo-alice scripts/run-cli.sh --server http://127.0.0.1:8137 grant \
  --grant '{"grantor":{"org":"demo","user":"alice"},"grantee":{"org":"demo","user":"bob"},"actions":["read"]}'
```

测试前不要保留给 Bob 的旧分享，也不要中途重启服务。Bob 第一次读取即成功时，
确认使用的是本例的新隔离服务；遇到连接失败则先检查服务和端口。

#### 6.2.3 SDK 分享与撤销

```bash
python examples/security/grant_sdk.py
```

预期 `PASS SDK Grant: denied/allowed/revoked`。示例显式配置认证器、标准授权器与
同源 Store；上下文统一由 `runtime.authenticate(...)` 获得，`grant` 的返回对象
可直接传给 `revoke`，不需要重新拼接参数。

#### 6.2.4 KeyStore、GrantStore 与 DelegationStore 存在哪里

| 存储对象 | 保存内容 | 当前默认与生命周期 |
|---|---|---|
| KeyStore | 主体身份、角色、Key 的验证材料与撤销状态 | 内置仅 `memory`；各进程独立，重启后原主体 Key 失效 |
| GrantStore | 授权 ID、资源范围、获权主体、动作、有效期与撤销状态 | 默认 `memory`；退出后记录丢失，可选择文件型 SQLite |
| DelegationStore | 委托、动作、期限、凭据/session 绑定与撤销状态 | 默认 `memory`；退出后记录丢失，可选择文件型 SQLite |

SQLite 的 `db_path: ":memory:"` 也不落盘。要保留分享和委托，先准备持久目录，再在
所选完整服务配置的 **`memory_api` 内**替换这些字段，保留原有认证等字段：

```yaml
authorizer:
  default:
    target: standard
    params: {grant_store: default, delegation_store: default}
grant_store:
  default: {target: sqlite, params: {db_path: ./state/grants.sqlite3}}
delegation_store:
  default: {target: sqlite, params: {db_path: ./state/delegations.sqlite3}}
```

路径相对服务工作目录，容器中使用持久卷，并配置目录权限、备份和静态保护。
持久化记忆不等于持久化分享；持久化 Grant/Delegation 也不等于持久化主体 Key。
部署方需要可靠的持久凭据时，应补充符合 KeyStore 契约的插件和受信管理入口。
不要靠重启代替正式撤销。

### 6.3 空间内容权限与治理权限

空间场景使用 `space_aware` 授权器和 `cloud` 引擎，完整配置见
[space.yml](../../../examples/security/space.yml)。两类空间角色分别设置：

| 角色类别 | 可选角色 | 作用 |
|---|---|---|
| 内容权限 | none / viewer / contributor / editor | 无权、只读、可贡献且仅改删本人作品、可改删空间内作品 |
| 治理权限 | none / manager / owner | 管理成员与策略；owner 还能删除空间，不自动获得正文读取权 |
| 组织角色 | USER / ADMIN / ROOT | 组织与系统管理，不等同于空间 manager |

```bash
python examples/security/space.py
```

预期 `PASS space: create/member/write/read`：组织管理员建空间，Alice 作为空间主人
添加 Bob，再验证内容访问。`cloud` 在此是本地引擎名，不需要远端云服务。

HTTP 添加只读成员时，正文示例为：

```json
{"org":"demo","space":"team","member":{"scope":{"user":"bob"},"content_role":"viewer","governance_role":"none"}}
```

调用者仍要有成员管理权限。Python SDK 的枚举字段使用 `SpaceContentRole` 与
`SpaceGovernanceRole`，HTTP JSON 则传对应字符串。

### 6.4 隔离验收

除了单条 `get`，也应使用 Bob 身份做检索和列表测试，确认不会返回未授权的 Alice
正文；授权后仅能执行获授动作；撤销后重新请求应恢复拒绝。始终记录测试 Key 对应
身份、目标 Scope、记忆 ID 和授权 ID，避免换错数据而得到误导结果。
查询隔离的内部契约见 [S10](../../specs/S10-security.md)，无需调整内部过滤器完成验收。

## 7. Delegation：让 agent 有限期代用户工作

Alice 让会议助手在接下来 30 分钟内代写会议结果。助手使用自己的身份、凭据和
session；服务检查 Alice 的委托是否仍有效，不需要把 Alice 的个人 Key 交给助手。

### 7.1 与 Grant 的区别

| 对比 | Grant | Delegation |
|---|---|---|
| 用途 | 让 Bob/agent 访问一部分资源 | 让 agent 在一次有限任务中代表用户工作 |
| 调用身份 | 获权者本人 | 仍为 agent，不变成用户 |
| 有效期 | 可以不设到期时间 | 必须有未来的有限到期时间 |
| 作者归属 | 不因分享把调用者变成资源主人 | 代写可归属委托人，同时记录实际 agent |
| 撤销入口 | `grant/revoke` 管理分享 ID | 受信服务端管理流程撤销委托 ID |

仅需让助手读共享资料时可以用 Grant；需要表达有限代理任务时用 Delegation。
两者独立撤销；已绑定委托失效的请求会拒绝，不回退尝试普通 Grant。

### 7.2 建立委托需要哪些信息

部署方确认用户同意与可授出范围后，为 agent 分配独立凭据/session，记录委托人、
agent、动作和到期时间，并绑定凭据指纹。允许动作仅为 `read/write/update/delete`，
不转交分享或管理能力；空间场景还需确认 Alice 当前有相应内容权并限制 `allowed_spaces`。

目前没有公共 `/v1/delegate` 或 CLI/MCP 委托管理入口。普通调用者只提交自己的
凭据和业务参数，不在头部或 JSON 填 `delegation_id`、`acting_user` 来选择身份。

### 7.3 完整代写与撤销测试

```bash
python examples/security/delegation.py
```

[delegation.py](../../../examples/security/delegation.py) 是受信服务端测试装配，包含完整
配置，自动计算凭据指纹与 UTC 加 30 分钟的到期时间，不需手填日期或拼接空间示例。
不需要 Root Key，也不连接前面的 HTTP 服务。

预期四行 `PASS`：真实调用者仍是 `agent1/session1`；代写内容的作者归属 Alice，
同时记录 agent；管理代码撤销后，撤销前已认证的旧上下文也被拒绝；Alice 仍能读取
原内容。脚本只创建本进程的测试任务，正式任务需在实际服务的同一委托 Store 中管理。

## 8. 入口资源保护与传输

| 配置/限制 | 使用者要做什么 | 预期行为 |
|---|---|---|
| `token_bucket.capacity/refill_per_sec/max_tracked` | 按调用量设置令牌桶容量和补充速度 | 超限返回 429；换 Key 不能绕过按来源地址的限流 |
| `semaphore.max_concurrent` | 配置认证并发预算 | 仅覆盖认证阶段，不是整个业务请求并发上限 |
| `binding_policy: loopback` | DEV 绑定 `127.0.0.1` | DEV 不允许对外监听 |
| HTTP 正文上限与读取超时 | 限制单次上传，默认正文 4 MiB、读取 30 秒 | 超限返回 413，超时终止请求 |
| 入站 TLS | 由部署反向代理终止 TLS，并限制后端直连 | 对外传输凭据与正文时加密；HTTP listener 本身无原生 TLS 配置 |

代理后多个客户端可能共享来源地址，需据此规划限流；服务不依赖客户端伪造的
`X-Forwarded-For` 改变来源。Store/模型的出站 TLS 配置与服务入站 TLS 分别设置。
遇到 429 应调整调用速率或容量，不增加无限排队。详细资源保护设计见
[F04](../../features/common/F04-security-interfaces-and-encryption.md#8-资源保护与部署攻击面)。

## 9. KV 静态加密

KV 按键保存值，应用加密保护的是值中的正文，键与 Scope 仍可见。应用写入前加密、
读取后解密，正常 API 返回明文是预期行为。算法为 AES-256-GCM，密文使用 ENC1 信封。
此应用加密不依赖 Linux 卷工具。

在当前完整配置的 `memory_api` 内选择加密 KV，片段为：

```yaml
kv_store:
  raw: {target: memory}
  default:
    target: encrypted
    params: {raw_kv_store: raw, cryptography: default}
cryptography:
  default: {target: local, params: {key_provider: default}}
key_provider:
  default:
    target: local
    params:
      key_env: AGENT_MEMORY_ENCRYPTION_ROOT_KEY
      key_file: ""
      create_key_file: false
      key_epoch: 1
```

根密钥使用独立的 32 字节材料，环境变量接受 64 字符 hex 或对应 base64。仅配置
cryptography 组件不会加密端口；必须配置装饰器或第 10.2 节统一开关。

| 保护范围 | 不自动保护 |
|---|---|
| 经过 encrypted KV 端口的值 | KV key、Scope、全文/向量/图等索引、媒体文件、Markdown、审计库、模型出站数据 |

密钥错误、密文篡改、归属不匹配或混入明文都会读取失败；没有 `allow_plaintext`
旁路。迁移旧明文应停写、备份，再通过加密端口重写并验证，不能简单把明文字节复制
到加密后端。检索和模型调用仍可能处理明文，需另做权限、TLS 和部署保护。

保留并安全备份持久数据对应的根密钥。local provider 的 `rotate()` 只在内存中维护
新旧密钥代，不能作为生产跨重启轮换方案；生产轮换需要可持久管理多代材料的 provider。
算法、密文绑定和后端扩展契约见 [S10](../../specs/S10-security.md) 与
[S06](../../specs/S06-storage.md)。

## 10. 全后端静态加密

KV/FS 使用应用内容加密；ES 等需要查询明文的索引和其他持久文件使用部署侧磁盘、
卷或后端静态加密。统一开关要求相关持久位置有保护记录，不会把索引改成密态检索。

**仓库内置 Docker 加密部署只支持 Linux 宿主机，Docker daemon 与加密卷需在同一宿主。**
Windows、macOS 与 Docker Desktop 宿主目录挂载不适用这套启动器；Windows 可运行
应用加密示例，完整 ES 加密部署安排在第 10.5 节要求的 Linux 测试机。

### 10.1 ES（Elasticsearch）怎样加密

ES 需要接收明文来分词、建立索引和执行查询。仓库方案加密 **ES 数据目录所在的卷**，
不是将查询字段变成应用密文：

```text
记忆正文 → 应用 ENC1 加密 → KV 保存密文
检索内容 → ES 建立索引 → ES 数据文件 → Linux 加密卷 → 磁盘保存密文
```

Linux 方案使用 LUKS2/dm-crypt 加密卷，卷内为 ext4。解锁挂载后 ES 正常读写，关闭卷
后不能把镜像当普通 ES 文件系统读取。**正常 ES 查询仍返回正文，不表示静态加密失败**；
它不阻止有运行中 ES 访问权限的人查询。HTTPS 仅保护传输，填写 `at_rest` 仅记录部署
核查，都不会自动开启 ES 磁盘加密。

| 数据 | 保护方式 |
|---|---|
| KV 与 message_store 内容 | 应用 ENC1 加密 |
| memory/local FS 媒体内容 | 应用加密整个对象 |
| ES、Milvus、pgvector、graph/fusion 等持久索引 | 运维落实加密卷或后端静态加密，并核对实际位置 |
| 审计、控制 SQLite、Markdown、shadow、缓存和备份 | 核查各自实际落盘位置与保护范围；不从 KV 加密推定已覆盖 |

### 10.2 统一开关与本地 API 验收

完整配置见 [encryption.yml](../../../examples/security/encryption.yml)，核心全局字段为：

```yaml
globals:
  storage_encryption_enabled: true
  storage_encryption_cryptography: default
```

配置还需定义 `cryptography.default` 和对应 key_provider。本地验收：

```bash
python examples/security/encryption.py
```

预期 `PASS encrypted API: write/read/search as DEV USER`。此例与其他 SDK 示例统一
使用 `assemble_server_runtime`、标准授权和 DEV USER。临时根密钥只用于该内存示例，不用于重启读取原有持久数据。

生产根密钥可通过专用环境变量或私密文件提供；同一份持久数据始终保留对应密钥。
开关默认 false，只接受布尔值或字符串 true/false；改开关和 provider 后必须重启。

同一个原始 KV/FS 后端不能绑定两套不同加密 provider；否则无法确定用哪套密钥读写，
服务会拒绝启动。统一开关启用时，也不能对 KV/FS 设置 `new_instance=true` 来绕过
共享包装。使用默认共享实例并保持 provider 引用一致即可，不必自行重复加密。
关闭统一开关不会解密历史数据，也不会移除显式配置的 encrypted 装饰器。

### 10.3 持久化索引的部署声明

内置 Linux 启动器检查挂载后生成声明。连接已有远端 ES 等后端时，部署人员先核查
远端已经落实静态加密，再在当前完整配置的 `memory_api` 内记录，例如：

```yaml
fulltext_store:
  default:
    target: elasticsearch
    params:
      hosts: https://es.example.com:9200
      at_rest:
        method: backend
        reference: ops/change-123/encryption-verification
        bindings: {hosts: "https://es.example.com:9200"}
```

`method` 为 `volume`（已核查加密卷）或 `backend`（后端已开启静态加密）；`reference`
填写实际核查记录；`bindings` 对应真实地址或目录。示例域名和记录编号须替换。

每个持久端口分别核查，包括 L0/L1。内存与 `:memory:` 无落盘声明要求。使用可动态
提供配置的 ConfigSource 时，不能覆盖已核查的受保护连接，也不能热改地址；需要
搬迁数据库或目录时，先重新核查新位置，更新配置后重启。这样避免连接已换到未加密
位置，而服务仍沿用旧的保护声明。后端位置契约见 [S10](../../specs/S10-security.md)。

### 10.4 FS 配置与使用限制

统一开关会包装受管的 memory/local FS。只启用某个 FS 时，可在当前 `memory_api`
配置内显式选择：

```yaml
fs_store:
  default:
    target: encrypted
    params:
      inner: {target: local, params: {root: ./encrypted-assets}}
      cryptography: default
      max_plaintext_bytes: 67108864
      max_ciphertext_bytes: 0
```

默认明文上限 64 MiB，`max_ciphertext_bytes: 0` 表示明文限额加 4 KiB，不是无限。
目前整体缓冲一个对象，测试大文件时需考虑内存占用；读取流使用后关闭。
路径和 Scope 仍可见。MemoryAPI 尚未提供资产上传/下载产品流程，存储集成者从
`manager.fs(name)` 使用端口；接口与稳定对象标识要求见 [S06](../../specs/S06-storage.md)。

### 10.5 在 Linux 测试机部署与验收

以下命令在**仓库根目录、Linux 宿主机终端**执行，不在容器内执行。
先准备 Python >=3.11、PyYAML、同宿主 Docker Compose，以及 `cryptsetup`、
`findmnt`、`lsblk`、`mkfs.ext4`、`tune2fs`。运维需提供两份独立的私密文件，
权限为 600：`/secure/volume.key` 用来解锁磁盘卷；`/secure/application.key` 存放
64 字符 hex 应用根密钥，用于 KV/FS ENC1。它们不能互相替代，也不等于认证 Key。

**步骤一：准备新测试卷。** 下面以全新 20 GiB 测试镜像为例；路径必须不存在。
若已经有符合要求的挂载，可跳过 init/open，直接 check。init 失败留下的同名镜像
应先由运维排查，不能对已有镜像或数据目录重新格式化。

```bash
sudo python -m deploy.encryption.volume init \
  --image /srv/agent-memory.luks --key-file /secure/volume.key --size-gib 20
sudo python -m deploy.encryption.volume open \
  --image /srv/agent-memory.luks --key-file /secure/volume.key \
  --mountpoint /srv/agent-memory-encrypted
sudo python -m deploy.encryption.volume check /srv/agent-memory-encrypted
```

**步骤二：配置并启动。** 以 `online` profile 为例，将
`deploy/docker/online/.env.example` 复制为同目录 `.env`，按该 profile 的部署说明
填写模型等原有参数，再添加：

```dotenv
STORAGE_ENCRYPTION_ENABLED=true
STORAGE_ENCRYPTION_ROOT=/srv/agent-memory-encrypted
STORAGE_ENCRYPTION_APPLICATION_KEY_FILE=/secure/application.key
```

这三个变量由部署启动器读取，不是 HTTP 参数。启动器会生成应用所需的
`globals.storage_encryption_enabled`、provider 和后端保护声明；只修改 `.env`
再直接运行原 `docker compose up` 不会给旧 named volumes 加密。

```bash
# 准备后端镜像，检查器需要读取镜像中实际运行用户的 UID。
docker compose --project-directory deploy/docker/online \
  -f deploy/docker/online/docker-compose.yml pull
sudo python -m deploy.encryption.compose --profile online --action check
# 仅首次、确认是全新数据布局时使用 --fresh-data；不执行旧数据迁移。
sudo python -m deploy.encryption.compose --profile online --action up --fresh-data
sudo python -m deploy.encryption.compose --profile online --action ps
```

`check` 会准备目录、配置与应用密钥文件，不是纯只读命令。它检查 ES 等实际使用的
子目录和 Compose 挂载，而不只是根路径是否加密。后续启动去掉 `--fresh-data`；
换用 `local` 或 `postgres` 时，同步替换 profile、目录和该 profile 的原有参数。

**步骤三：验收并关闭。** 正常写入、检索应可读；停止服务、重新打开同一卷并重启后，
原有数据仍应可读。把加密根目录换成普通目录时，部署检查应拒绝启动。ES 正常查询
返回明文不能用于判断磁盘是否加密；部署人员还应核对实际 ES 挂载，并验证关闭卷后
镜像不能按普通文件系统读取。不要在运行中的数据库目录上删除或改写文件做测试。

```bash
sudo python -m deploy.encryption.compose --profile online --action down
sudo python -m deploy.encryption.volume close --mountpoint /srv/agent-memory-encrypted
```

已有 named-volume/KV/FS 数据应先停写、备份，再离线迁移，不能用 `--fresh-data`
掩盖旧数据尚未迁移。索引文件迁入加密卷；KV/FS 明文则必须从 raw 端口读取、经
encrypted 端口写入，不能只复制明文字节。备份、导出、swap、快照和密钥备份也需
各自保护。运行期间应用不持续监控管理员是否替换了挂载。

配置声明与本地 API 验证不能替代真实 LUKS+Docker 部署验收。生产部署还需核查实际
挂载、迁移、恢复和密钥轮换路径。

## 11. 审计与日志

### 11.1 普通审计

普通审计用于追踪调用者、动作、目标和允许/拒绝结果。默认 SQLite `:memory:`
不保存到磁盘；需要留存时在 `memory_api` 内配置文件型审计器：

```yaml
audit:
  default: {target: sqlite, params: {db_path: ./state/audit.sqlite3}}
```

部署人员准备目录、访问权限、保留期、备份和静态加密。普通审计不自动提供防篡改
证明；日志脱敏也不能保证任意自定义字符串中都没有秘密。问题报告不附 Key、根密钥、
密码验证材料或完整用户正文。

### 11.2 审计完整性

完整性审计为记录增加链式校验，帮助识别内容或链接被改动。完整服务配置见
[audit.yml](../../../examples/security/audit.yml)：它同时选择 API Key 认证、标准授权、
文件型 SQLite、独立的审计 key_provider、`audit_integrity: chained_hmac` 和验证预算。
审计密钥与认证 Key、内容加密根密钥分别使用独立材料。

先运行无需手工配置路径的完整测试：

```bash
python examples/security/audit.py
```

预期两行 `PASS`：HTTP 和 CLI 均成功调用真实服务并读到 `status=clean`；没有配置
外部锚点时 `anchor.checked=false`。数据库放在系统临时目录，测试结束后清理。

需要本地 CLI 单独验证时，设置环境变量并使用同一份完整配置。以下命令只生成全新
测试库；已有库应保留原审计密钥与路径。Bash：

```bash
export AGENT_MEMORY_ROOT_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export AGENT_MEMORY_API_KEY="$AGENT_MEMORY_ROOT_API_KEY"
export AGENT_MEMORY_AUDIT_ROOT_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export AGENT_MEMORY_AUDIT_DB="$(python -c 'import tempfile; from pathlib import Path; print(Path(tempfile.mkdtemp(prefix="agent-memory-audit-")) / "audit.sqlite3")')"
scripts/run-cli.sh --config examples/security/audit.yml verify_audit \
  --after_sequence 0 --page_size 1000 --max_samples 20 --anchor_policy if_configured
```

PowerShell：

```powershell
$env:AGENT_MEMORY_ROOT_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:AGENT_MEMORY_API_KEY = $env:AGENT_MEMORY_ROOT_API_KEY
$env:AGENT_MEMORY_AUDIT_ROOT_KEY = python -c "import secrets; print(secrets.token_hex(32))"
$env:AGENT_MEMORY_AUDIT_DB = Join-Path ([IO.Path]::GetTempPath()) ("audit-" + [guid]::NewGuid() + '.sqlite3')
python -m jiuwen_memory_entry.cli --config examples/security/audit.yml verify_audit `
  --after_sequence 0 --page_size 1000 --max_samples 20 --anchor_policy if_configured
```

**CLI 参数使用下划线**，不是 `--after-sequence` 等连字符写法。HTTP 使用
`POST /v1/verify_audit`，凭据在 Bearer 中，正文为：

```json
{"after_sequence":0,"page_size":1000,"max_samples":20,"anchor_policy":"if_configured"}
```

验证要求受信 ROOT，普通 ADMIN 不够。SDK 使用已从实际 Runtime 认证得到的
`security`，对 `api.verify_audit` 传相同四个关键字参数。返回 200 不等于完整性通过，
必须读取结果：

| 字段/状态 | 应如何判断 |
|---|---|
| `clean` | 本次检查范围内一致，不等于防回滚已完成 |
| `tampered` | 内容或链接不一致，应保存证据并排查 |
| `incomplete` | 缺记录、证据、历史密钥代或未验证到快照链头 |
| `rollback_suspected` | 与可信外部锚点冲突 |
| `unsupported` | 未配置完整性 provider，不能当作通过 |
| `high_water_mark` | 本次连续成功验证到的序号 |
| `truncated` | 错误样本超限，不是已扫描全链的标记 |

外部锚点目前没有内置 WORM/KMS/云产品或自动发布作业。`if_configured` 无锚点时
不做外部核对，`skip` 跳过核对，`required` 无锚点会报配置错误。没有外部锚点时，
本地链不能独立识别“合法历史前缀和链头一起回滚”，也不替代审计库的静态加密。

审计故障时不要直接删除旧库重试；先备份记录，并核对业务操作是否已产生结果，
避免盲目重复写入。旧无签名库没有自动补签迁移流程。故障恢复、轮换与并发追加的
实现说明统一见 [F04](../../features/common/F04-security-interfaces-and-encryption.md#7-审计与完整性边界)。

## 12. MCP 与服务生命周期

先安装 MCP 依赖：

```bash
python -m pip install -e ".[mcp,security]"
python examples/security/mcp_stdio.py
```

预期两行 `PASS`：真实 stdio 会话用 Root 成功调用 `memory_add`；因为 api-key.yml
没有完整性 provider，`memory_verify_audit` 返回 `unsupported`。此脚本自动为 MCP
子进程设置相同 Root/客户端 Key，不向模型输出密钥。

**本例 API Key 配置在 required 模式使用全新内置 memory KeyStore 时，只有 Root Key 可用于引导。**
另一个进程签发的普通 Key 不会出现在新 MCP 进程里，见第 6.2.4 节。普通用户部署
必须先完成同进程凭据初始化或持久 KeyStore 集成，不能长期分发 Root Key 代替。

stdio 手动启动示例（与第 4.2 节相同的 api-key.yml，配置文件已在仓库中）：

```bash
export AGENT_MEMORY_ROOT_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export AGENT_MEMORY_API_KEY="$AGENT_MEMORY_ROOT_API_KEY"
JIUWEN_MEMORY_MCP_AUTH_MODE=required scripts/run-mcp.sh examples/security/api-key.yml
```

服务使用标准输入输出等待 MCP 客户端消息，适合由 MCP 客户端启动和管理。PowerShell：

```powershell
$env:AGENT_MEMORY_ROOT_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:AGENT_MEMORY_API_KEY = $env:AGENT_MEMORY_ROOT_API_KEY
$env:JIUWEN_MEMORY_MCP_AUTH_MODE = 'required'
python -m jiuwen_memory_entry.mcp_server examples/security/api-key.yml
```

仅本机 DEV 测试可用 `JIUWEN_MEMORY_MCP_AUTH_MODE=dev scripts/run-mcp.sh`，默认身份为 USER。
Streamable HTTP 方式：

```bash
MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8138 \
  JIUWEN_MEMORY_MCP_AUTH_MODE=required scripts/run-mcp.sh examples/security/api-key.yml
```

HTTP 客户端逐请求发送自己的 Bearer，不回退到服务进程的客户端环境变量。stdio 则
读取 `AGENT_MEMORY_API_KEY`。MCP 工具参数不需要也不接受客户端自述 actor/role。

自己装配 SDK Runtime 时，在 `finally` 中调用 `runtime.close(wait=True)`；服务入口
由宿主停止接收并等待任务结束。后台任务仍受权限、委托到期和撤销约束；重启不是
延长授权的办法。`wait=False` 也不保证立即返回，不要在工作尚未结束时关闭其存储。
内部关闭顺序与共享资源规则见 [F04](../../features/common/F04-security-interfaces-and-encryption.md#41-dreaming-与生命周期)。

## 13. 错误与排障

| 现象 | 优先检查 |
|---|---|
| 401 | 请求 Key、DEV selector、撤销状态；Trusted 的网关 Key、身份头与主体注册 |
| 403 | 实际身份与目标 Scope、组织边界、Grant/Delegation、空间内容和治理权限 |
| 400 | 参数类型、必填字段、撤销 ID；审计验证异常也可能映射为 400 |
| 409 | 冲突或部分完成；核对已完成部分，再决定重试 |
| 429 | 调用速率、代理汇聚后的来源地址、认证预算与服务容量 |
| 503 | 安全配置被覆盖、后端不可用、已关闭连接或工作队列故障 |
| 413/上传超时 | 正文大小和读取时间 |
| `verify_audit: unsupported` | 是否启用且引用正确完整性 provider |
| 加密读取失败 | 原密钥和密钥代、归属范围、密文完整性；不要生成新钥覆盖旧钥 |

先保留请求 ID、测试身份、目标 Scope、记忆/授权/委托 ID、预期和实际结果、脱敏配置
及最小复现。不要通过改 JSON 角色、关闭授权/校验、删除数据库或重开私有连接来绕过。
记录只包含排障所需信息，不附原始 token、密码 hash、根密钥或完整业务正文。

## 14. 配置检查与维护

1. 确认选中的认证器和授权器引用正确，生产关闭 DEV。
2. 用 USER 凭据验证本人允许、他人拒绝、分享和撤销，不用 Root 代替普通用户测试。
3. 分别核对正文、索引、媒体、Markdown/shadow、审计与备份的保护范围。
4. 保存密钥与恢复流程，持久化前区分主体凭据、授权记录和业务数据。
5. 验证委托到期、撤销和空间权限下降；停止服务前等待任务结束。
6. 审计验证读取结果状态与外部锚点检查结果，不把 HTTP 成功视为验证通过。

随附示例由 CI 直接执行，说明见 [examples/security/README.md](../../../examples/security/README.md)。
这些本地演练不代替真实 Linux/Docker/ES、密钥托管、迁移和恢复验收。
