# Pipeline API 参考

## 获取流水线评论

```bash
PYTHONIOENCODING=utf-8 curl -s -H "PRIVATE-TOKEN: <token>" \
  "https://gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{N}/comments"
```

**要点：**
- PR 评论用 `/pulls/{n}/comments`，不能用 `/issues/{n}/comments`（404）
- Token 用 `PRIVATE-TOKEN` header，不用 query param (`?access_token=`)
- Windows 设 `PYTHONIOENCODING=utf-8` 防中文乱码
- Windows 用 `python` 而非 `python3`

## 提取 + 解析（单条命令）

以下脚本直接输出结构化结果，避免 HTML 进入上下文：

```bash
PYTHONIOENCODING=utf-8 curl -s -H "PRIVATE-TOKEN: <token>" \
  "https://gitcode.com/api/v5/repos/{owner}/{repo}/pulls/<N>/comments" \
  | python -c "
import sys, json, re
data = json.load(sys.stdin)
# 找最新含流水线的评论
for c in reversed(data):
    body = c.get('body', '')
    if '流水线' not in body:
        continue
    # 提取运行状态行
    status_line = re.search(r'(已终止运行|运行失败|运行成功|运行中)', body)
    print('Status:', status_line.group(1) if status_line else 'unknown')
    # 解析表格
    rows = re.findall(r'<tr>(.*?)</tr>', body, re.DOTALL)
    for row in rows:
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(tds) < 3:
            continue
        stage = ''
        if 'rowspan' in row:
            stage = re.sub(r'<[^>]+>', '', tds[0]).strip()
        task = re.sub(r'<[^>]+>', '', tds[-3]).strip()
        status = tds[-2]
        if '9989' in status: s = 'PASS'
        elif '10060' in status: s = 'FAIL'
        elif '128346' in status: s = 'WAIT'
        elif '129000' in status: s = 'SKIP'
        else: s = '?'
        if task and task not in ('>>', '阶段', '任务名', '状态', '详情'):
            label = f'[{stage}] ' if stage else ''
            print(f'  {label}{task}: {s}')
    break
"
```

## HTML Entity 对照

| Entity | 含义 | 图标 |
|--------|------|------|
| `&#9989;` | 通过 | ✅ |
| `&#10060;` | 失败 | ❌ |
| `&#128346;` | 等待中 | 🟫 |
| `&#129000;` | 跳过/终止 | 🟰 |
