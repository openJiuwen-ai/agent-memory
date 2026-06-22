# GitCode Pull Request API 参考

## 认证

GitCode API 支持两种认证方式：

1. **PRIVATE-TOKEN Header**（推荐）
   ```bash
   curl -H "PRIVATE-TOKEN: your_token" https://gitcode.com/api/v5/...
   ```

2. **access_token 查询参数**
   ```bash
   curl "https://gitcode.com/api/v5/...?access_token=your_token"
   ```

## Pull Request 端点

### 获取 PR 详情

```
GET /repos/:owner/:repo/pulls/:number
```

响应字段：
- `number`: PR 编号
- `title`: 标题
- `body`: 描述
- `state`: 状态 (open/closed)
- `user.login`: 作者
- `head.label`: 源分支
- `base.label`: 目标分支
- `mergeable`: 是否可合并
- `merged`: 是否已合并
- `additions/deletions/changed_files`: 变更统计
- `html_url`: 网页链接

### 获取 PR 文件变更

```
GET /repos/:owner/:repo/pulls/:number/files
```

响应字段（每个文件）：
- `filename`: 文件路径
- `status`: 状态 (added/modified/removed/renamed)
- `additions/deletions`: 行数变更
- `changes`: 总变更数
- `patch`: diff 内容

### 获取 PR Commits

```
GET /repos/:owner/:repo/pulls/:number/commits
```

响应字段（每个 commit）：
- `sha`: commit hash
- `commit.message`: 提交消息
- `commit.author.name/email/date`: 作者信息
- `author.login`: GitCode 用户名

### 获取 PR 评论

```
GET /repos/:owner/:repo/pulls/:number/comments
```

响应字段（每条评论）：
- `id`: 评论 ID
- `body`: 评论内容
- `user.login`: 评论者
- `created_at/updated_at`: 时间
- `path`: 关联文件（行内评论）
- `position`: 行位置（行内评论）

### 创建 PR 评论

有两种方式：

**方式 1: Issue 评论（推荐）**

```
POST /repos/:owner/:repo/issues/:number/comments
```

Body:
```json
{
  "body": "评论内容（支持 Markdown）"
}
```

**方式 2: PR 评论（可指定行）**

```
POST /repos/:owner/:repo/pulls/:number/comments
```

Body:
```json
{
  "body": "评论内容",
  "commit_id": "abc123",
  "path": "file.py",
  "position": 10
}
```

参数说明：
- `commit_id`: commit SHA
- `path`: 文件路径
- `position`: 文件中相对位置

### 创建 PR

```
POST /repos/:owner/:repo/pulls
```

Body:
```json
{
  "title": "PR 标题",
  "head": "fork_owner:branch",
  "base": "<主仓主干分支，取自 upstream.base_branch>",
  "body": "PR 描述"
}
```

## GitLab Merge Request 端点

GitCode 也支持 GitLab 风格的 API：

```
GET /repos/:owner/:repo/merge_requests/:number
POST /repos/:owner/:repo/merge_requests/:number/comments
```

功能与 `/pulls` 端点类似。

## 限流

- 50 次/分钟
- 4000 次/小时
- 超限返回 HTTP 429

## 错误响应

```json
{
  "message": "error message",
  "errors": [...]
}
```

常见错误码：
- 400: 请求参数错误
- 401: 未授权
- 403: 无权限
- 404: 资源不存在
- 422: 验证失败
- 429: 请求频率超限
