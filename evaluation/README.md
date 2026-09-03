# LongMemEval 本地端到端一键测评

本目录提供从 SSH 环境整理出的 LongMemEval 测评实现，不依赖官方旧版
`evaluation/benchmark`、`evaluation/core`、`evaluation/metrics`、
`evaluation/scripts` 或 `evaluation/smoke_test`。所有适配代码、模型代理、后端服务
编排、数据路径和运行配置均位于新的 `evaluation/longmemeval` 及公共目录中，不修改
agent-memory 记忆内核。

mini 数据只减少输入 turn 数；写入、抽取、Redis、Milvus、Elasticsearch、检索、
答案生成和 AnswerJudge 均走正式链路，不使用 Mock。

## 一键运行

### Windows

在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$PWD\evaluation\run.ps1"
```

### Linux / SSH

```bash
sh evaluation/run.sh
```

脚本会依次：

1. 创建或复用仓库根目录的 `.venv`，安装项目和测评依赖；
2. 清理 `agent-memory-evaluation` 专用 Docker 卷；
3. 复用标准端口上已经健康的 Redis、Elasticsearch、Milvus，并拉起缺失服务；
4. 拉起 SSH 同款的 GLM 抽取代理、回答/判分代理和 BGE embedding 代理；
5. 执行 mini 样本的写入、抽取、检索、回答和判分；
6. 把结果写入 `evaluation/outputs/longmemeval/<时间>/`。

首次运行需要下载镜像和 Python 包。Docker Desktop 建议预留约 6 GB 内存。默认端口
为 `6379`、`9200`、`19530`、`9091`、`18937`、`18938`、`18939`。

## 环境配置

复制模板：

```powershell
Copy-Item evaluation\environment\.env.example evaluation\environment\.env
notepad evaluation\environment\.env
```

至少填写以下上游配置：

```text
LONGMEMEVAL_CHAT_UPSTREAM_URL
LONGMEMEVAL_CHAT_MODEL
LONGMEMEVAL_CHAT_API_KEYS
LONGMEMEVAL_EMBED_UPSTREAM_URL
LONGMEMEVAL_EMBED_MODEL
LONGMEMEVAL_EMBED_API_KEY
```

`LONGMEMEVAL_CHAT_API_KEYS` 支持多个 Key，使用英文逗号分隔。真实 `.env` 已被 Git
忽略，不得提交 API Key 或外部模型 URL。

## mini 数据

默认输入是：

```text
evaluation/datasets/longmemeval/longmemeval_mini.json
```

该样本从标准 `longmemeval_oracle.json` 数组下标 `232` 机械截取，
`question_id=89527b6b`，保留 1 个 Oracle session、2 个原始 turn、原始问题和答案。
没有改写保留字段。来源和 SHA-256 见
`evaluation/datasets/longmemeval/README.md`。

## 测评口径

- 按 `dialogue_turn` 写入，相邻 user/assistant 合并为一轮；
- 单轮超过 4096 字符时按句切分，并携带有界前文；
- 只写 `answer_session_ids` 指定的 Oracle session；
- `infer=true`，抽取后验证 `retain_source=false`；
- Redis 保存真源，Milvus 向量召回，Elasticsearch 关键词召回；
- weighted RRF 使用 vector `2.0`、keyword `1.0`、`k=20`，关闭 rerank；
- 召回 Top-200，再按 `50 → 10 → 20` 三个 cutoff 生成答案；
- 使用 LongMemEval 专用 answer prompt 和 AnswerJudge prompt；
- 抽取代理关闭 GLM thinking，回答和 Judge 代理保留 thinking；
- 代理对成功但内容为空的 completion 执行重试。

## 修改题目范围和并行参数

编辑 `evaluation/config.yml`：

```yaml
longmemeval:
  question_ids: []
  sample_indices: [0]
  max_questions: 1
  concurrency: 1
```

`question_ids` 非空时按题号选择，否则使用 `sample_indices`；`max_questions: 0`
表示不限制。本阶段建议先保持 mini 和 `concurrency: 1`。

切换到标准数据集时，只替换数据路径和范围，不替换测评实现：

```yaml
longmemeval:
  data: datasets/longmemeval/longmemeval_oracle.json
  question_ids: []
  sample_indices: null
  max_questions: 0
```

完整标准数据默认被 `.gitignore` 排除，不会进入 PR。

## 兼容的直接入口

下面的命令只运行 Python 测评，不创建虚拟环境，也不拉起后端服务：

```powershell
.\.venv\Scripts\python.exe -m evaluation --config evaluation\configs\e2e_smoke.yml
```

从零执行端到端测试应使用 `evaluation/run.ps1`。

## 输出与日志

```text
evaluation/outputs/longmemeval/<时间>/result.json
evaluation/outputs/longmemeval/<时间>/artifacts/
evaluation/outputs/longmemeval/<时间>/*_proxy.log
evaluation/outputs/longmemeval/<时间>/*_proxy_audit.jsonl
```

运行产物、日志、缓存和真实 `.env` 均被忽略。

## 停止服务

保留数据卷：

```powershell
docker compose -f evaluation\environment\docker-compose.yml stop
```

停止容器并删除本测评专用卷：

```powershell
docker compose -f evaluation\environment\docker-compose.yml down --volumes
```

## 常见错误

- `docker` 命令不存在：安装并启动 Docker Desktop；
- `port is already allocated`：检查上述默认端口是否被其他服务占用；
- 缺少环境变量：检查 `evaluation/environment/.env`；
- 模型回复异常：检查输出目录的 `extract_proxy.log` 和
  `answer_judge_proxy.log`；
- embedding 异常：检查 `embed_proxy.log` 和 `embed_proxy_audit.jsonl`；
- 后端未健康：执行
  `docker compose -f evaluation/environment/docker-compose.yml ps`。

## PR 边界

需要提交新的 LongMemEval 源码、公共启动与环境模板、mini 数据和本文档。不得提交：

- `evaluation/environment/.env`；
- API Key、外部模型 URL；
- 完整标准数据；
- `evaluation/outputs/`、日志、审计记录、缓存和编译产物；
- `evaluation_legacy_backup/`、`evaluation_backup_*/`。
