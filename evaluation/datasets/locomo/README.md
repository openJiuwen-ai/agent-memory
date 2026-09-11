# LoCoMo 数据

将官方 `locomo10.json` 放在本目录：

`evaluation/datasets/locomo/locomo10.json`

`locomo_mini.json` 只用于本地冒烟，不代表正式分数。它由当前标准
`locomo10.json`（SHA-256：`79FA87E90F04081343B8C8DEBECB80A9A6842B76A7AA537DC9FDF651EA698FF4`）
机械截取得到：原始数组下标 `1`、`sample_id=conv-30`、`session_1` 的 `D1:1` 和 `D1:2`、
原始 `qa[0]`。对话、日期、问题、答案、证据、category 均未改写。

原样本中 `event_summary`、`observation`、`session_summary` 会引用被裁掉的 turn 或 session，
因此没有放入 mini；SSH LoCoMo v5 runner 也不读取这些派生标注字段。

mini 保留 2 个原始 turn 和原始 `qa[0]`（证据 `D1:2`）。它仍运行远端 v5 的完整
写入、抽取、Redis/Milvus/Elasticsearch、Top-200 检索、多 cutoff 生成与 Judge 链路。
mini SHA-256：`EC93C5CEC49BC444A7814D184C5E3E3279B96E833FA7D0E83C1225479545EDDF`。
