# F02 — 存储静态加密设计（KV、FS 与部署保护）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27；2026-09-07 按 PR1 收敛；2026-10-15 扩展 PR1-1 并统一归档 |
| 影响范围 | storage、common/Factory、config、API 装配、audit/permission 插件、deploy/encryption；S06/S07/S08/S10 |
| 测试基线 | KV/FS、插件替换、装配策略、部署检查单测；MemoryAPI 持久化与重装配集成测试，见「验证」 |
| Refs | #143 |

## 背景

PR1 已交付密码学提供者、密钥提供者与显式 EncryptedKVStore。加密收敛在字节存储边界，
避免在 add/search/get 等业务入口分别实现并漏接。FS 原计划单独交付；对旧实现评估发现
insert key 与返回 ref 不一定相等，必须先统一稳定对象标识，同时补齐保护标记与读写限额。

PR1-1 基于含已合入 PR1 的上游，将范围扩为全部现有后端的静态加密，分支为
`sec/storage-encryption`。KV/FS 使用应用层加密；全文、向量、图、融合与实体索引以及
Markdown 真源、SQLite 文档影子索引与独立审计/权限库使用部署侧磁盘、卷或后端加密。自行连接的远端服务由部署方启用，
项目提供配置契约和检查。保留现有 BM25、ANN、过滤与融合，不实现密态检索，也不新增
应用层「全量解密—检索—再加密」流程。相关存储加密决策统一维护在本文。

## 决策

### 1. KV 字节保护沿用 PR1

1. **`EncryptedKVStore` 是装饰器，不是物理后端。** 它包装显式配置的
   `raw_kv_store`；raw 后端可以是 memory、sqlite、redis 或其他 KV 实现。
2. **只加密 value。** `insert` / `update` 写入前加密，`get` / `mget` / `scan` 读取后
   解密；`list` 必须先解密，再做 MemoryUnit 过滤、计数、排序和分页。key 与 scope 保持
   可寻址，`exists` / `delete` / `scopes` 直接透传。
3. **算法和密钥管理不进 storage。** storage 只构造 `CryptoContext` 与稳定 AAD，并调用
   `CryptographyProvider`。provider 只能经独立 `KeyProvider` 获取密钥。
4. **AAD 绑定完整对象语义。** AAD 绑定
   `Scope(org/space/user/agent/session)`、KV key、用途和格式版本。用途由 key 前缀确定：
   `/memory/` 为 `memory_unit`，`/messages/` 为 `raw_message`，其他为 `kv_value`。
5. **严格失败关闭。** 非法信封、AAD 不匹配、未知密钥或任何解密异常统一转为
   `BackendError`；不返回密文、不跳过坏记录、不回退明文。是否加密由部署选择显式 encrypted Store 或启用统一开关表达，同一个装饰器没有 `allow_plaintext` 开关。
6. **具名依赖共享。** `raw_kv_store` 与 `cryptography` 都使用 Producer 具名引用。
   `SecurityRuntime` 若引用相同的 `cryptography` 名称，必须与存储取得同一对象。
7. **装配失败要早。** 缺少 `raw_kv_store`、引用自身或缺少密码学依赖时，在构建期拒绝；
   不能静默创建临时内存后端或透传 provider。


### 2. FS 稳定对象标识与严格边界

FSStore 增加可选的 object_id 能力：插入 key、返回 ref、后续寻址别名必须映射到同一
标识，update 不能改变标识。未知后端默认抛 UnsupportedCapabilityError，raw 使用
不受影响，encrypted 装配拒绝缺少能力的后端。LocalFS 解析并规范化相对物理路径，
InMemoryFS 以完整规范 ref 为标识，RoutingFS 转发到所选后端。

EncryptedFSStore 将五维 Scope、稳定 object_id、fs_object 用途和格式版本 1 绑定到
CryptoContext/AAD。它只委托 CryptographyProvider，不实现算法或读取密钥。
stat.size 返回密文大小，路径和 scope 不隐藏。

默认明文上限 64 MiB，默认密文上限为明文上限加 4 KiB，支持配置。写入有界循环读取
并在落盘前检查生成密文；读取执行 stat 早拒、实际流有界读取及解密后明文复核。
短读不能截断文件；原输入流由调用方管理，inner get 的流总是关闭，返回新明文 BytesIO。
明文、篡改、错误对象/Scope/密钥均失败关闭。

### 3. 统一开关与插件替换边界

`globals.storage_encryption_enabled` 默认 false，装配期严格接受 bool 或 true/false 字符串。
开启后 CompositeStoreManager 保护全部具名 KV/FS，含注入 KV、消息独立端口和公开 raw 别名；
Producer 共享缓存同步指向保护对象，KV/FS 的 new_instance=true 被拒绝。关闭开关保留显式
encrypted target，不自动拆除现有保护层。

保护层通过 StoreSecurity 的公开 encryption_binding 声明原始依赖、provider 和格式。
同一 raw 的公开别名复用同一个保护对象；不同 provider 或格式的冲突声明拒绝装配。
原生加密插件可直接声明已启用保护，核心不读取 `_raw`、`_inner`、`_encryption` 等私有状态。
自动包装通过 KvProducer/FsProducer 与 `storage_encryption_kv` / `storage_encryption_fs`
配置选择 target/params，默认 encrypted；raw 与共享 cryptography 依赖由策略显式注入。
第三方装饰器遵循相同契约即可参与复用，无须修改核心分支。

公共 StoreManagerProducer.resolve 对所有替代 manager 统一验证 encryption_inventory：
完整覆盖所声明的 capability、与公开端口对象一致、配置的 KV/FS 名字不得遗漏，所有字节
端口须启用并通过保护健康检查。manager 须保证领域数据面使用同一受保护依赖，不能在清单外
持有可写 raw；未实现清单契约的旧插件开启时拒绝装配，关闭时保持兼容。
独立的底层 Store/Producer 构造路径由调用方选择显式保护，不在通用 Factory 里隐式包装。

### 4. 持久化位置由插件声明，未知能力失败关闭

插件随 Producer.register 注册无副作用的 `at_rest_locations` 配置检查器，返回本实例
全部可切换位置的字段和值；没有可切换存储位置的纯内存或关闭插件才返回空映射。
位置可用 AtRestLocation(value, requires_declaration=False) 声明免部署证明，仍须进入连接守卫。
普通值默认要求部署证明，保留既有插件契约。注册表与 builder 一同替换，
同名插件不能继承旧 target 的豁免。配置层不维护具体 target 白名单，也不按 memory 名字、
缺少 hosts/endpoint 或未知 audit/permission target 猜测无需保护。

持久化实例的 params.at_rest 须有 method=volume/backend、非空 reference，以及与检查器
返回的全部 requires_declaration=true 位置逐项相等的 bindings。现有后端检查器覆盖 ES hosts（实体兼容 endpoint）、
Milvus uri、pgvector dsn、本地图 working_dir、融合后端 uri+working_dir、SQLite db_path、Markdown root。
开启统一加密时 Markdown 必须显式给出 root，影子索引必须显式给出 db_path（`:memory:`
仅免部署证明，仍需声明位置）；禁止依赖当前目录或由另一个命名空间派生数据库位置，以免晚绑定改变实际落盘点。
影子索引的 MemoryUnit JSON、FTS5 文本、sqlite-vec 向量和 SQLite 附属文件由同一卷保护，
保持现有检索逻辑；不在 Markdown 可编辑文本或索引字段上另加应用层密文。
SQLite 的 `:memory:` 只免部署声明，db_path 仍固定为 :memory:；影子索引及独立审计/权限
SQLite 均通过公开位置契约声明。初始 ConfigSource 覆盖或运行时从内存切到磁盘均拒绝，
不先写入后补查；已打开与尚未打开连接的路径一致，目标明文文件不得创建。
原先返回空映射会丢失此约束，不能把“内存免部署证明”等同于“位置可任意切换”。
所有具名/L0/L1 实例都纳入检查。新增持久化插件只需注册自身检查器，不改配置层。

这些声明是可信服务配置中的运营方核查记录，不是本进程证明了远端磁盘已加密。
StoreSecurity.enabled() 仍表达 Store 内部保护，外部保护通过部署声明及宿主检查表达。
EncryptionBoundConfigSource 拒绝绑定位置的初始 override 和后续热切换；当前同命名空间
共享 namespace.field 晚绑定键的实例须使用相同连接位置。更改位置需重核查并重装配。

### 5. Docker 三套 profile 使用真正加密的块存储

deploy/encryption/volume.py 交付 Linux LUKS2 镜像初始化、打开、关闭和检查入口。
init 用 O_EXCL 只创建新镜像，绝不格式化已有文件或块设备；卷密钥是独立的已有私密文件。
open 要求空挂载目录，失败收尾关闭本次 mapper。close 要求目录对应指定 mapper，先卸载。
check 使用 findmnt 与 lsblk inverse 拓扑，所有底层分支必须经过 crypt，普通盘、NFS、
未知设备或缺少检查工具一律拒绝；要求 ext4 内部 journal，避免仅检查主设备却漏掉
文件系统外部存储；控制文件也逐个检查，不能用单独的明文 file bind 绕过目录检查。
检查不执行格式化或自动挂载。

deploy/encryption/compose.py 根据 STORAGE_ENCRYPTION_ENABLED（默认 false）选择原始
profile 或加密 overlay。打开后先核查宿主挂载，再把三套 Compose 的持久化 named
volume 按容器 target 替换为加密根下 bind。覆盖 Redis、ES（全文/实体）、Milvus、
etcd、MinIO、PostgreSQL/pgvector、模型缓存；应用数据目录覆盖 SQLite 审计、权限、FS、Markdown 及文档影子索引，
配置 nano_graphrag/milvus_graph 时也必须迁入该应用目录。

生成配置明确打开应用开关；应用 ENC1 根密钥以私密文件固定保存于加密卷控制目录，
只读挂载给应用。首次启动要提供独立的 64 字符 hex 应用密钥，已有密钥不允许静默替换。
控制目录由 root 管理，数据目录读取固定后端镜像的实际 UID 后准备；命名用户解析使用
禁用网络、替换 entrypoint 的 id 命令，无法解析就拒绝。应用按仓库 Dockerfile 的 UID
10001 准备，不猜测第三方镜像均为 root。每个子目录再次核查实际挂载，
阻止 symlink/未加密子挂载绕过。bind.create_host_path=false 防止 Docker 自动造普通目录。

启动前检查 docker compose config --format json 的真实合并结果，并再次核查挂载。
要求本地 Unix Docker endpoint 和匹配当前宿主的 daemon 名称，不能用本机卷检查结果为
远端 Docker daemon 或 Desktop VM 的数据目录背书。
Compose 按 target 合并 volumes，源 named volumes 即使仍在顶层声明也不承载这些服务数据。
配置生成/检查不打印 DSN、凭据、密钥或完整展开后的 Compose model。

本地卷证据不能用于被 .env 改到外部的 ES/Milvus/PG；外部地址必须有相符的运营方声明。
后端 native 搜索不改写，也不新增数据库服务器解密代码。

### 6. 开关不是历史数据迁移

首次加密 up 需 --fresh-data 明确确认新的数据布局；初始化状态留在控制目录。
现有文件型 SQLite 和 graph/FS 原目录不自动移动或覆盖。已有 named volume 数据需离线
备份和迁移，KV/FS 明文需通过显式 raw→encrypted 读写转成 ENC1，不能直接复制明文字节。
关闭开关也不自动解密历史 ENC1 数据，操作前必须停止写入、确认数据布局及备份。
备份/导出离开加密挂载后，需要目的地自己的静态加密策略。

### 7. 静态门禁整改与部署工具边界

2026-10-15 的未解决问题导出包含本 PR 的 17 项、9 类规则；整改仍在本文归档，不新增存储文档。

| 规则 | 项数 | 整改 |
|---|---:|---|
| G.LOG.02 | 2 | 两个 CLI 使用标准 logging 报告成功，不输出配置或凭据 |
| G.PRM.03 | 1 | 文件描述符归创建方所有，文本流不接管底层关闭；finally 保证正常、包装失败和写入异常均关闭 |
| G.EDV.05 | 4 | Docker 与卷工具均先从系统目录解析为绝对路径，排除 profile PATH 和当前目录 |
| G.CTL.03 | 1 | 保护依赖检查分成缺省、类型与语义校验，各条件保持原拒绝语义 |
| G.CLS.11 | 3 | 测试使用公开 encryption_binding 与 registration_scope，不读取插件私有状态 |
| G.CLS.01 | 1 | 第三方 KV 测试替身正常调用父类初始化 |
| G.FIO.05 | 2 | mapper 设备路径使用 pathlib 拼接 |
| G.PSL.03 | 1 | CLI 从仓库根用 python -m 运行，移除 sys.path 前插，避免模块遮蔽 |
| G.FNM.03 | 2 | 用 DeploymentLocation 统一传递关联的 data_root/profile，两函数参数均不超过五个 |

保留密钥文件 0600、O_EXCL 与配置文件 O_NOFOLLOW 等边界；描述符关闭覆盖 fdopen 自身抛错，
不能只依赖文本流的 with。临时注册隔离同时恢复 builder 和检查器（含嵌套及异常），避免测试
替换泄漏到后续装配；实例缓存仍由原 reset_all 生命周期管理，此边界不作并发注册事务。
这些规则的本地代码核对与回归不等于远端扫描已重跑通过，最终门禁结果由项目扫描确认。

## 配置示例（显式 KV 装饰器）

```yaml
cryptography:
  default:
    target: local
    params:
      key_provider: default

key_provider:
  default:
    target: local
    params:
      key_file: ~/.agent-memory/security/master.key

kv_store:
  raw:
    target: sqlite
    params:
      db_path: agent_memory.db
  default:
    target: encrypted
    params:
      raw_kv_store: raw
      cryptography: default
```

上一版 `security.<name>.target=local` 与 `params.security` 会在配置解析期自动迁移一个发布
周期并发出 `DeprecationWarning`。旧 `allow_plaintext` 不会恢复明文回退。

统一开关通常在原有配置上增加：

```yaml
globals:
  storage_encryption_enabled: true
  storage_encryption_cryptography: default
  storage_encryption_kv: {target: encrypted}
  storage_encryption_fs: {target: encrypted, params: {max_plaintext_bytes: 67108864}}
fulltext_store:
  default:
    target: elasticsearch
    params:
      hosts: https://es.internal:9200
      at_rest:
        method: backend
        reference: operator:es-volume-check
        bindings: {hosts: 'https://es.internal:9200'}
```

## 拒绝的方案

- 在 MemoryAPI/Engine 各动词分别加密，或各物理后端重复实现：边界易遗漏并重复 AAD/密钥逻辑。
- 所有索引字段直接套 AES-GCM：破坏 BM25/ANN/排序和原生过滤，需独立密态检索设计。
- 仅在内置 CompositeStoreManager 执行开关：替代 manager 会静默绕过统一策略。
- 通过具体类、私有字段或 target 白名单识别插件：替换实现需改核心，且未知持久化能力可能漏检。
- 只保护 default 或在 Factory 递归自动包装：会漏具名别名，或破坏原始依赖共享与包装层次。
- 把布尔声明当远端加密证明、热切换开关或解密失败回退明文：均无法保持失败关闭。
- 自动格式化磁盘、移动旧数据或混读历史明文：部署初始化和离线迁移必须独立执行。

## 验证

- 真实 ENC1 provider 验证 KV 与 FS 的密文落盘、读取、AAD 绑定、篡改/错误密钥拒绝；
  KV scan/list 解密后按既有语义筛选分页；FS 覆盖别名、短读、大小边界与流关闭。
- 替代 manager 在公开 assemble/assemble_runtime 下不能绕过开关；无清单、清单不完整、
  未保护端口与对象不一致均拒绝。符合契约的替代 manager 可正常服务加密端口，关闭时兼容旧插件。
- 公开 assemble_runtime 回归验证 :memory: 初始覆盖拒绝、连接建立前后动态切盘拒绝、
  未产生磁盘文件且原内存数据保留；关闭开关时切换行为兼容。第三方混合位置声明与
  多实例共享键冲突也纳入回归。
- 第三方包装器经 Producer/配置选择，raw 别名复用单层保护；未知审计、权限和实体插件拒绝，
  插件自行声明新位置字段后可通过，冒用 memory 名称不能继承内存豁免。
- Docker local/online/postgres 规划覆盖全部持久化 mount、设备拓扑和远端连接核查，
  新建文档后端显式生成受保护 root/db_path，旧位置要求离线迁移；已有密钥不替换。
  密钥替换用例显式 chmod(0600)，确保 POSIX 下命中预期拒绝分支。
- MemoryAPI 集成测试验证正常搜索、SQLite raw value 为 ENC1，以及重装配后的 KV/FS 读取。
- 同步上游文档后端后的公共装配/插件/KV/FS/部署/文档与持久化组合回归：
  387 passed、1 skipped；本 PR 修改的 Python 文件 Ruff 与 git diff --check 通过。
- 内存位置约束修复后完整 unit：2794 passed、9 skipped，无失败。前轮 Windows HTTP 早拒绝路径
  曾出现 WinError 10053；HTTP 模块独立复跑 67 passed，本次完整重跑也已通过。
- 上游新增的符号链接越界测试仅在 Windows 缺少创建权限（WinError 1314）时明确跳过；
  日志降级告警测试显式开启测试捕获传播，消除应用日志初始化引入的运行顺序依赖。
  其余 8 项跳过沿用可选后端依赖等环境条件。
- WSL 原生 POSIX 权限探针直接加载生产 prepare_files 函数，验证 0600 源密钥命中
  “application key differs from the existing deployment key” 拒绝分支并保留原密钥；另直接加载 private_text_file，
  验证实际 0600、O_NOFOLLOW 和 fdopen 失败后描述符关闭。该环境未安装 pytest，
  因此此项为隔离函数探针，不声称 Linux 全量 pytest 已通过。

## 已知遗留

- Windows 开发宿主未完成真实 Linux LUKS+Docker 服务栈验收和性能测试；单测拓扑不能替代部署验收。
- 远端保护依赖运营方核查，不自动配置远端主机，也不监控运行中宿主挂载被管理员替换。
- key、Scope、FS 路径和数量仍可见；整盘/卷加密负责完整文件保护，备份/导出需目的地自身保护。
- FS 尚未接入 MemoryAPI 资产上传/下载流程；分块加密不在本 PR。
- LocalKeyProvider 轮换历史仍仅在进程内；跨重启轮换需持久化托管 provider。
- 未声明完整保护清单的动态 manager（含手工 RoutingStoreManager）开启统一开关时拒绝；
  需要插件保证所有可切换依赖受保护后实现清单契约，不能只核查当前选中的 raw 后端。
