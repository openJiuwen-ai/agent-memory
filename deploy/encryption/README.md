# PR1-1 静态加密部署

默认关闭；检索算法保持原样。KV/FS 使用 PR1 的 ENC1 provider，索引、独立 SQLite 和
后端文件使用 Linux LUKS/dm-crypt 卷。三套 local/online/postgres profile 共用启动器。

## 准备加密卷

宿主需要 Linux、Python >=3.11、PyYAML、Docker Compose、cryptsetup、findmnt、lsblk
和 mkfs.ext4/tune2fs。加密启动与目录权限准备由宿主管理员执行；Docker daemon 必须位于同一宿主。
命令统一从仓库根以 `python -m deploy.encryption.compose` 或 `python -m deploy.encryption.volume` 运行，避免修改 sys.path。
外部工具从宿主系统目录解析为绝对路径；不使用 profile 的 PATH 或当前目录寻找特权命令。
已有 ext4（内部 journal）dm-crypt 挂载可以直接使用，无须执行 init。多设备文件系统和
外部 journal 需另行核查，本启动器拒绝自动声明其完整覆盖。以下命令均在仓库根执行。
启动器同时要求本地 Unix Docker endpoint 和与宿主相符的 daemon 名称，拒绝远端 Docker
context、socket proxy 到其他宿主及 Docker Desktop VM 借用本机加密挂载的证据。

先通过密钥管理流程提供私密的卷密钥文件 `/secure/volume.key`（chmod 600），以及
独立的应用根密钥 `/secure/application.key`（64 个 hex 字符，32 bytes，chmod 600）。
不要把这两份文件写进仓库、Compose 环境变量或公开日志。需要保留两份密钥的受保护备份。

```bash
# 只允许创建尚不存在的镜像；已有镜像/块设备不会被格式化。
sudo python -m deploy.encryption.volume init \
  --image /srv/agent-memory.luks --key-file /secure/volume.key --size-gib 20
sudo python -m deploy.encryption.volume open \
  --image /srv/agent-memory.luks --key-file /secure/volume.key \
  --mountpoint /srv/agent-memory-encrypted
sudo python -m deploy.encryption.volume check /srv/agent-memory-encrypted
```

镜像容量固定，按实际索引、WAL/日志、MinIO 对象及增长预留空间；本工具不自动扩容。
init 失败会保留镜像供排查，不能对同名镜像重试格式化。open 要求挂载点为空，避免遮盖旧数据。

## 一个开关选择加密部署

复制目标 profile 的 `.env.example` 为 `.env`，填写原有模型凭据，再配置：

```dotenv
STORAGE_ENCRYPTION_ENABLED=true
STORAGE_ENCRYPTION_ROOT=/srv/agent-memory-encrypted
STORAGE_ENCRYPTION_APPLICATION_KEY_FILE=/secure/application.key
```

```bash
# 先准备 profile 的固定后端镜像；检查器读取镜像实际 UID，不猜测目录应由 root 拥有。
docker compose --project-directory deploy/docker/online \
  -f deploy/docker/online/docker-compose.yml pull
# 校验实际 encrypted block topology、准备私密配置并检查 Compose 合并结果。
sudo python -m deploy.encryption.compose --profile online --action check
# 首次使用新布局须明确确认，命令不会迁移任何旧 named volume。
sudo python -m deploy.encryption.compose --profile online --action up --fresh-data
# 后续重启沿用加密卷中保存的应用密钥；不同密钥会被拒绝。
sudo python -m deploy.encryption.compose --profile online --action up
sudo python -m deploy.encryption.compose --profile online --action ps
sudo python -m deploy.encryption.compose --profile online --action down
sudo python -m deploy.encryption.volume close --mountpoint /srv/agent-memory-encrypted
```

把 online 改成 local 或 postgres 即可。关闭开关时启动器使用原始 Compose profile。
开关是部署/装配选择，不能代替数据迁移；关闭后不得拿 raw KV/FS 直接读旧 ENC1 数据。
加密索引连接热切换也需要重新核查并重装配。

原 `docker compose up` 仍用于默认关闭的部署；开启时应使用此启动器。仅修改 .env
不会给原 named volumes 加密，应用发现缺少持久化索引保护声明时会拒绝装配。

## 覆盖与证据边界

| 数据 | 加密位置 |
|---|---|
| Redis 数据、AOF | redis-data 的 encrypted bind |
| ES 正文、L0/L1、实体索引及 metadata | es-data 的 encrypted bind |
| Milvus 向量/metadata、本地文件 | milvus-data 的 encrypted bind |
| Milvus etcd 元信息与 MinIO 对象 | etcd-data / minio-data 的 encrypted binds |
| PostgreSQL KV、pgvector、索引与 WAL | postgres-data 的 encrypted bind |
| FS 原件、配置后的本地图与融合图 | application bind；FS 内容另有 ENC1 |
| SQLite audit/permission（含 space_aware）、Grant/Delegation | application bind |

启动器按后端镜像的实际 UID 准备数据目录；命名用户通过禁用网络、替换 entrypoint 的 id
命令解析，无法解析就拒绝；应用镜像按仓库 Dockerfile 的固定 UID 10001 准备。
启动器检查每个子目录及已有控制文件确实位于加密块设备上，普通目录、普通磁盘、NFS、
非 ext4 文件系统、外部 journal 和缺失工具均拒绝；
它不自动格式化、挂载或解密后端数据库。应用的 StoreSecurity.enabled() 只表达应用
Store 内部保护，部署保护声明是另外的责任边界。

如果连接改到远端服务，不能借用本地卷的证据。远端运营方需在对应实例中给出已核查记录：

```yaml
fulltext_store:
  default:
    target: elasticsearch
    params:
      hosts: https://es.example.com:9200
      at_rest:
        method: backend
        reference: ops/change-123/encryption-verification
        bindings:
          hosts: https://es.example.com:9200
```

reference 是可信配置中的运营方声明，应用不能据此证明远端磁盘真的加密。
bindings 必须与实际位置相同；每个具名和 L0/L1 实例分别声明。除 ES hosts，现有字段为
Milvus uri、pgvector dsn、图 working_dir、融合 uri+working_dir、SQLite db_path、Markdown root。统一加密时 Markdown 与影子 SQLite
须显式声明 root/db_path，避免默认派生路径绕过绑定。SQLite :memory: 只免部署证明，
db_path 仍须固定，拒绝 ConfigSource 初始覆盖及运行时切换到磁盘。
内存后端和 SQLite :memory: 无持久化加密要求。

## 已有数据迁移与验收

停写并备份后再迁移：索引/图/数据库持久化文件迁入加密卷，KV/FS 明文通过 raw 读取、
encrypted 写入转为 ENC1。不得直接把明文复制到 encrypted 逻辑端口，也不得用开关
静默创建新数据库冒充迁移。导出、快照和备份目的地需要自己的加密保护。

部署后用同一批查询比较加密前后的结果、P50/P95/P99、吞吐和冷启动；检查卸载后镜像
无法直接读取数据库内容，重开卷/重启容器后旧数据可读，wrong key 和普通目录不能启动。
当前环境完成离线规划/设备检查模拟、真实 KV/FS 测试及 Ubuntu/WSL 普通目录拒绝探针；
WSL 缺少 cryptsetup 和 Docker，LUKS+Docker 全栈验收仍需在完整 Linux 环境执行。

参考：[Docker volume 合并规则](https://docs.docker.com/reference/compose-file/merge/)、
[Linux dm-crypt](https://docs.kernel.org/admin-guide/device-mapper/dm-crypt.html)。
