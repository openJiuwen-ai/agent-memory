# agent-memory2.0

## LongMemEval 本地端到端测评

本分支提供独立的 LongMemEval 测评实现。mini 数据只缩小对话体量，记忆写入、
抽取、Redis、Elasticsearch、Milvus、检索、回答和 AnswerJudge 均走完整链路。

在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$PWD\evaluation\run.ps1"
```

首次运行前，需要把 `evaluation/environment/.env.example` 复制为 `.env` 并填写模型
服务配置。详细配置、数据范围和故障排查见
[evaluation/README.md](evaluation/README.md)。
