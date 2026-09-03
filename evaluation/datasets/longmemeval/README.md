# LongMemEval 数据

正式 Oracle-500 数据：

`evaluation/datasets/longmemeval/longmemeval_oracle.json`

`longmemeval_mini.json` 从上述标准数据数组下标 `232` 原样复制，`question_id=89527b6b`，
包含 1 个 Oracle session、2 个 turn 和 1 道问题，字段和值没有改写。它只缩小数据体量；
写入、抽取、三类存储、检索、答案生成与 AnswerJudge 均与正式测评一致。

- 标准数据 SHA-256：`821A2034D219AB45846873DD14C14F12CFE7776E73527A483F9DAC095D38620C`
- mini SHA-256：`5A91ECDCFCCDAFCADB69FF457E51A1D775551DEB4C26286AA3736C8E92004B52`
