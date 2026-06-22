# GitCode API v5 速查

Issue Resolver 使用的 GitCode API 端点参考。
Base URL: `https://gitcode.com/api/v5`
认证: `access_token` query param

## Issue

### 获取 Issue 列表

```
GET /repos/:owner/:repo/issues
```

参数:
- `state`: open / closed / all（默认 open）
- `labels`: 逗号分隔的标签名
- `assignee`: 指派人用户名
- `page`, `per_page`: 分页

### 获取 Issue 详情

```
GET /repos/:owner/:repo/issues/:number
```

响应关键字段:
- `number`, `title`, `body`, `state`
- `labels[].name`
- `assignee.login`
- `html_url`, `created_at`, `updated_at`

### 更新 Issue

```
PATCH /repos/:owner/issues/:number
```

Body:
- `repo`: 仓库名（必填）
- `title`, `body`, `state`, `assignee`: 可选

### 获取 Issue 评论

```
GET /repos/:owner/:repo/issues/:number/comments
```

参数: `page`, `per_page`

响应关键字段:
- `id`, `body`, `user.login`, `created_at`

### 创建 Issue 评论

```
POST /repos/:owner/:repo/issues/:number/comments
```

Body: `{"body": "评论内容"}`

### Issue 标签

```
POST /repos/:owner/:repo/issues/:number/labels
```

Body: `["label1", "label2"]`（JSON 数组）

```
DELETE /repos/:owner/:repo/issues/:number/labels/:name
```

## Pull Request / MR

### 创建 PR

```
POST /repos/:owner/:repo/pulls
```

Body:
- `title`: PR 标题
- `head`: 源分支（跨仓库格式 `fork_owner:branch`）
- `base`: 目标分支
- `body`: PR 描述

### 获取 PR 列表

```
GET /repos/:owner/:repo/pulls
```

参数: `state`, `head`, `base`, `page`, `per_page`

### 获取 PR 文件变更

```
GET /repos/:owner/:repo/pulls/:number/files
```

## 限流

- 50 次/分钟，4000 次/小时
- 超限返回 429，需等待后重试
- `gitcode_client.py` 已内置自动重试逻辑
