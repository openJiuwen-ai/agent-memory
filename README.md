# agent-memory2.0

## 本地端到端测评：从零到跑通

下面的流程使用 `evaluation/` 内独立整理的 SSH 同款测评实现。LongMemEval 与
LoCoMo 的 runner、Prompt、AnswerJudge 和输出格式彼此分开；mini 数据只缩小
对话体量，记忆抽取、Redis、Elasticsearch、Milvus、检索、回答和判分均走真实链路。

### 运行前确认

1. 在仓库根目录打开 PowerShell。
2. 启动 Docker Desktop，并确认 Docker 使用 Linux containers。
3. 确认本机可访问 `.env` 中配置的模型服务。

当前工作区已经有从 SSH 环境整理出的真实配置：

```text
evaluation\environment\.env
```

该文件包含敏感信息，已被 Git 忽略，不要提交或粘贴到日志。如果把 `evaluation/`
复制到新克隆的官方仓库，且 `.env` 不存在，才执行：

```powershell
Copy-Item evaluation\environment\.env.example evaluation\environment\.env
notepad evaluation\environment\.env
```

然后填入与 SSH 机器一致的 URL、API Key、模型和 embedding 配置。

### 一键执行 LongMemEval mini

默认配置是 LongMemEval。在仓库根目录只执行这一条：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$PWD\evaluation\run.ps1"
```

脚本会自动：

1. 创建或复用 `.venv` 并安装依赖；
2. 清理本测评专用的 Docker 卷；
3. 复用标准端口上已经健康的 Redis、Elasticsearch、Milvus，并由 Compose 拉起缺失的后端；
4. 拉起 LongMemEval 所需的 GLM 与 embedding 本地代理；
5. 对标准数据集机械截取出的 1 个 mini 样本执行写入、检索、Answer 和 Judge；
6. 把结果写入 `evaluation\outputs\longmemeval\<时间>\`。

LongMemEval mini 使用标准数据 `longmemeval_oracle.json` 下标 `232` 的原始样本，
`question_id=89527b6b`，保留 1 个 Oracle session、2 个 turn 和原始答案。

### 一键执行 LoCoMo mini

打开 `evaluation\config.yml`，只把第一行：

```yaml
benchmark: longmemeval
```

改为：

```yaml
benchmark: locomo
```

再执行同一条一键命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$PWD\evaluation\run.ps1"
```

LoCoMo mini 来自标准 `locomo10.json` 的 `sample_id=conv-30`，保留原始
`D1:1`、`D1:2` 和 evidence 为 `D1:2` 的原始 `qa[0]`。结果位于
`evaluation\outputs\locomo\<时间>_local-smoke\`。

### 关于你刚才执行的旧命令

旧 README 曾给出：

```powershell
.\.venv\Scripts\python.exe -m evaluation --config evaluation\configs\e2e_smoke.yml
```

现在保留了该路径作为兼容入口，因此它不再报“启动配置不存在”。但这条命令只启动
Python 测评，不负责创建虚拟环境，也不负责拉起 Redis、Elasticsearch 和 Milvus。
本地从零测试请使用上面的 `evaluation\run.ps1`。

### 修改题目范围与并行配置

统一修改：

```text
evaluation\config.yml
```

LongMemEval 常用项是 `question_ids`、`sample_indices`、`max_questions`、
`concurrency`；LoCoMo 常用项是 `conversation_ids`、`max_sessions`、`max_turns`、
`max_qa`、`concurrency`。

本阶段是功能性验证，建议先保持 mini 与 `concurrency: 1`。切换完整标准数据集的
路径和范围，以及两套远端测评口径的详细说明，见
[evaluation/README.md](evaluation/README.md)。

### 常见错误

- `docker` 命令不存在：安装并启动 Docker Desktop。
- `port is already allocated`：检查 `6379`、`9200`、`19530`、`9091`、
  `18937`、`18938`、`18939` 中被占用的端口。
- `缺少环境变量`：检查 `evaluation\environment\.env`，不要只修改仓库根目录 `.env`。
- 模型或 embedding 异常：检查本轮输出目录中的 `*_proxy.log` 和
  `*_proxy_audit.jsonl`。
- 后端未健康：执行：

```powershell
docker compose -f evaluation\environment\docker-compose.yml ps
```

### 停止服务

保留测评数据卷：

```powershell
docker compose -f evaluation\environment\docker-compose.yml stop
```

停止容器并删除本测评专用卷：

```powershell
docker compose -f evaluation\environment\docker-compose.yml down --volumes
```
