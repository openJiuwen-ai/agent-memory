# agent-memory 本地端到端一键测评

这个目录可以整体复制到官方 `agent-memory` 仓库中使用，不依赖旧版
`evaluation/`，也不依赖 `evaluation_legacy_backup/`。所有适配代码、模型代理、
后端服务编排、数据路径和运行配置都在本目录内；不会修改记忆内核。

默认运行 **LongMemEval mini**。mini 只减少输入 turn 数，写入、抽取、Redis、
Milvus、Elasticsearch、检索、答案生成和 AnswerJudge 都走正式链路，不是 Mock。

## 一、一键运行

### Windows（在仓库根目录执行）

```powershell
powershell -ExecutionPolicy Bypass -File evaluation\run.ps1
```

### Linux / SSH

```bash
sh evaluation/run.sh
```

脚本会自动完成以下事情：

1. 创建或复用仓库根目录的 `.venv`，按
   `evaluation/environment/requirements.txt` 安装项目和测评依赖；
2. 清空本测评专用的 Docker 数据卷；
3. 复用标准端口上已经健康的 Redis、Elasticsearch、Milvus，由 Compose 拉起缺失的
   服务并等待健康检查通过；
4. 按 `evaluation/config.yml` 选择 mini 数据和题目范围；
5. LongMemEval 额外自动拉起 SSH 同款的 GLM 抽取代理、GLM 回答/判分代理和
   BGE embedding 校验代理；
6. 跑完整端到端链路并把结果写入 `evaluation/outputs/`。

> 每次执行会删除 **`agent-memory-evaluation` 专用 Docker 卷**，不会删除其他 Docker
> 项目或宿主机数据。标准端口上已存在且健康的外部服务会直接复用，其数据不会被脚本清理。

首次运行需要下载镜像和 Python 包，会比后续运行慢。请为 Docker Desktop 至少预留
约 6 GB 内存。默认服务端口是 `6379`、`9200`、`19530`、`9091`、
`18937`、`18938`、`18939`。

## 二、环境配置

真实配置文件是：

```text
evaluation/environment/.env
```

当前工作区中的该文件已经从 SSH 测评环境整理出实际使用的模型 URL、API Key、
embedding、reranker 和存储配置，并被 `.gitignore` 排除。不要把它提交、粘贴到日志或
发给无关人员。

如果把 `evaluation/` 复制到一个全新仓库而没有携带 `.env`，执行：

```powershell
Copy-Item evaluation\environment\.env.example evaluation\environment\.env
```

然后填入与 SSH 机器一致的值。LongMemEval 的 `LONGMEMEVAL_CHAT_API_KEYS` 支持多个
Key，使用英文逗号分隔。

## 三、切换 LongMemEval / LoCoMo

只编辑 `evaluation/config.yml` 第一项：

```yaml
benchmark: longmemeval
```

改成：

```yaml
benchmark: locomo
```

再执行同一条一键命令即可。也可以临时覆盖而不改文件：

```powershell
.\.venv\Scripts\python.exe -m evaluation --config evaluation\config.yml --benchmark locomo
```

临时覆盖命令只运行 Python，不会自动拉起后端；正常本地测试优先使用 `run.ps1`。

旧版根 README 曾使用 `evaluation/configs/e2e_smoke.yml`。该路径现作为兼容入口保留，
但同样只运行 Python；从零端到端测试仍应使用 `run.ps1`。

## 四、mini 数据不是伪造样本

### LongMemEval mini

- 来源：`evaluation/datasets/longmemeval/longmemeval_oracle.json`；
- 原数组下标：`232`；
- `question_id`：`89527b6b`；
- 保留 1 个 Oracle session、2 个原始 turn、1 道原始问题；
- 整个样本原样复制，没有改写对话、时间、答案或证据字段。

### LoCoMo mini

- 来源：`evaluation/datasets/locomo/locomo10.json`；
- 原数组下标：`1`，`sample_id=conv-30`；
- 保留 `session_1` 的原始 `D1:1`、`D1:2`；
- 保留原始 `qa[0]`，其证据是 `D1:2`；
- 仅裁掉与本题无关的 session、turn 和派生摘要，没有改写保留字段。

数据来源和 SHA-256 记录在两个数据子目录的 README 中。

## 五、两套测评方式明确分开

### LongMemEval

- 按 `dialogue_turn` 写入，相邻 user/assistant 合并为一轮；
- 单轮超过 4096 字符时按句切分，并携带有界前文；
- 只写 `answer_session_ids` 指定的 Oracle session；
- `infer=true`，抽取后执行 `retain_source=false` 契约；
- Redis 保存真源，Milvus 做向量召回，Elasticsearch 做关键词召回；
- weighted RRF：vector `2.0`、keyword `1.0`、`k=20`，关闭 rerank；
- 先召回 Top-200，再按 `50 → 10 → 20` 三个 cutoff 生成答案；
- 使用 LongMemEval 专用 answer prompt 和 AnswerJudge prompt；
- 抽取代理关闭 GLM thinking；回答和 Judge 代理保留 thinking；代理对成功但内容为空的
  completion 进行重试。

### LoCoMo

- 保留 SSH 上的 `test_locomo_v5.py` 写入/检索/生成/判分逻辑；
- 每条消息独立写入，携带 session 日期，`infer=true`；
- 默认 `RAW_FALLBACK=0`、`RAW_ALWAYS=0`、`DATE_PREFIX=0`；
- Redis、Milvus、Elasticsearch 和 weighted RRF 参数与 SSH 配置一致，关闭 rerank；
- 召回 Top-200，再按 `10 → 20 → 50 → 200` cutoff 生成答案并判分；
- GLM 调用按 SSH `.env` 设置 `DISABLE_THINKING=1`；
- mini 默认只跑 1 个 conversation、2 个 turn、1 道 QA（数据中只有这些内容）。

两套 runner、prompt、配置和输出格式分别位于 `longmemeval/` 与 `locomo/`，没有使用
一个通用适配器替代其中任何一套。

## 六、修改测试范围

所有常用项都在 `evaluation/config.yml`。

LongMemEval 可修改：

```yaml
longmemeval:
  question_ids: []       # 非空时优先按 question_id 选题
  sample_indices: [0]    # mini 中只有下标 0
  max_questions: 1       # 0 表示不限制
  concurrency: 1
```

LoCoMo 可修改：

```yaml
locomo:
  conversation_ids: [0]
  max_sessions: 0
  max_turns: 5
  max_qa: 2
  concurrency: 1
```

## 七、切换到标准数据集

只换数据路径和范围，不换测评实现。

LongMemEval：

```yaml
longmemeval:
  data: datasets/longmemeval/longmemeval_oracle.json
  question_ids: []
  sample_indices: null
  max_questions: 0
```

LoCoMo：

```yaml
locomo:
  data: datasets/locomo/locomo10.json
  conversation_ids: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
  max_sessions: 0
  max_turns: 0
  max_qa: 0
```

本阶段是功能验证，建议先保持 mini 和 `concurrency: 1`，确认端到端通过后再放大。

## 八、结果在哪里

LongMemEval：

```text
evaluation/outputs/longmemeval/<时间>/result.json
evaluation/outputs/longmemeval/<时间>/artifacts/
evaluation/outputs/longmemeval/<时间>/*_proxy.log
evaluation/outputs/longmemeval/<时间>/*_proxy_audit.jsonl
```

LoCoMo：

```text
evaluation/outputs/locomo/<时间>_local-smoke/test_result_v5.json
evaluation/outputs/locomo/<时间>_local-smoke/test_metrics_v5.json
evaluation/outputs/locomo/<时间>_local-smoke/run_meta.json
evaluation/outputs/locomo/<时间>_local-smoke/run.log
evaluation/outputs/locomo/<时间>_local-smoke/engine.log
```

这些运行产物全部被忽略，不会混入测评源码或数据目录。

## 九、停止本地服务

保留数据卷：

```powershell
docker compose -f evaluation\environment\docker-compose.yml stop
```

停止并删除本测评专用容器和卷：

```powershell
docker compose -f evaluation\environment\docker-compose.yml down --volumes
```

## 十、常见错误

- `docker API permission denied`：先启动 Docker Desktop，并确认当前用户有权访问 Docker。
- `port is already allocated`：关闭占用上述端口的旧服务，再重跑。
- `缺少环境变量`：检查 `evaluation/environment/.env`，不要只修改根目录 `.env`。
- LongMemEval 模型异常：先看本轮的 `extract_proxy.log`、`answer_judge_proxy.log`；空回复
  重试次数在 `evaluation/config.yml` 的 `proxy_retry_attempts`。
- embedding 异常：看 `embed_proxy.log` 和 `embed_proxy_audit.jsonl`。
- 后端未健康：执行
  `docker compose -f evaluation/environment/docker-compose.yml ps` 查看具体服务。

## 十一、复制到新的官方仓库

1. 克隆官方 `agent-memory`；
2. 删除新仓库原来的 `evaluation/`；
3. 把本目录完整复制为新仓库的 `evaluation/`；
4. 确认两个标准数据文件或 mini 文件位于 `evaluation/datasets/` 对应子目录；
5. 配置 `evaluation/environment/.env`；
6. 在新仓库根目录执行一键命令。

运行路径只导入官方仓库的 `jiuwen_memory` 内核包；不会导入旧 evaluation、备份目录或
当前工作区的其他测评脚本。

## 十二、PR 提交边界

需要提交：

- `evaluation/__init__.py`、`evaluation/__main__.py`、`evaluation/config.yml`、
  `evaluation/configs/e2e_smoke.yml`；
- `evaluation/longmemeval/` 与 `evaluation/locomo/` 中的独立 runner、Prompt、指标、
  代理和内核配置；
- `evaluation/shared/`、`evaluation/environment/docker-compose.yml`、
  `evaluation/environment/requirements.txt` 与 `.env.example`；
- `evaluation/setup.*`、`evaluation/run.*`、README；
- 两个数据目录的 README 和 `longmemeval_mini.json`、`locomo_mini.json`。

不得提交：

- `evaluation/environment/.env`，以及任何真实 API Key、外部模型 URL；
- 完整标准数据 `longmemeval_oracle.json`、`locomo10.json`；
- `evaluation/outputs/`、`__pycache__/`、`*.pyc`、日志、审计记录和结果文件；
- 根目录的 `evaluation_legacy_backup/`、`evaluation_backup_*/`；
- 与本次测评无关的仓库改动。

`.gitignore` 已覆盖以上本地产物。`.env.example` 中 API 地址与密钥保持为空，仅保留
`127.0.0.1` 本地 Redis、Elasticsearch、Milvus 地址，方便一键拉起本地后端。
