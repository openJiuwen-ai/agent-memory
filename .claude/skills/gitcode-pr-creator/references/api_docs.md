# GitCode Pull Request API 文档

## API端点

### 创建Pull Request

**端点**: `POST /api/v5/repos/:owner/:repo/pulls`

**描述**: 在GitCode仓库中创建一个新的Pull Request

## 必需参数

| 参数名 | 类型 | 说明 |
|--------|------|------|
| `access_token` | string | 用户授权令牌 |
| `title` | string | PR标题 |
| `head` | string | 源分支，格式：`namespace:branch` 或 `branch` |
| `base` | string | 目标分支 |

## 可选参数

| 参数名 | 类型 | 说明 |
|--------|------|------|
| `body` | string | PR描述内容 |
| `milestone_number` | integer | 里程碑编号 |
| `labels` | string | 标签，逗号分隔 |
| `issue` | integer | 关联的Issue编号 |
| `assignees` | string | 指派人，逗号分隔的用户名 |
| `testers` | string | 测试人员，逗号分隔的用户名 |
| `prune_source_branch` | boolean | 合并后是否删除源分支 |
| `squash` | boolean | 是否squash合并 |

## 参数格式说明

### head 参数

- **跨仓库PR**: 使用格式 `namespace:branch`，例如 `username:feature-branch`
- **同仓库PR**: 直接使用分支名，例如 `feature-branch`

### base 参数

- 目标分支名称，例如 `main` 或 `dev`

## 网页版创建MR链接格式

**重要**: GitCode使用GitLab风格的Merge Request（MR），而不是GitHub风格的Pull Request（PR）。

### 基本格式

从fork仓库创建MR到上游仓库：

```
https://gitcode.com/:source_owner/:source_repo/merge_requests/new?merge_request[source_branch]=:source_branch&merge_request[target_project_id]=:target_owner/:target_repo&merge_request[target_branch]=:target_branch
```

### URL参数说明

- `:source_owner/:source_repo` - 源仓库（您的fork）
- `merge_request[source_branch]` - 源分支
- `merge_request[target_project_id]` - 目标仓库（格式：owner/repo）
- `merge_request[target_branch]` - 目标分支

### 简化方法（推荐）

1. **访问fork仓库的分支页面**：
   ```
   https://gitcode.com/:source_owner/:source_repo/-/tree/:branch_name
   ```
   
2. **从fork仓库创建MR**：
   ```
   https://gitcode.com/:source_owner/:source_repo/merge_requests/new?merge_request[source_branch]=:branch_name
   ```
   GitCode会自动识别upstream仓库作为目标。

### URL查询参数

可以通过查询参数预填表单字段：

- `merge_request[title]` - MR标题
- `merge_request[description]` - MR描述
- `merge_request[milestone_id]` - 里程碑ID
- `merge_request[label_ids][]` - 标签ID数组

## 示例

### 跨仓库MR链接（推荐方式）

从fork仓库创建MR：

```
https://gitcode.com/username/repo/merge_requests/new?merge_request[source_branch]=feature-branch
```

### 访问分支页面（最简单）

```
https://gitcode.com/username/repo/-/tree/feature-branch
```

然后点击页面上的"Create merge request"按钮。

### 带查询参数的MR链接

```
https://gitcode.com/username/repo/merge_requests/new?merge_request[source_branch]=fix-bug&merge_request[title]=Fix%20bug&merge_request[description]=This%20fixes%20the%20bug
```

## 注意事项

1. 分支必须存在于对应的仓库中
2. 跨仓库MR需要源仓库是目标仓库的fork
3. 用户需要有相应的权限才能创建MR
4. URL中的特殊字符需要进行URL编码
5. **GitCode使用GitLab风格的Merge Request，不是GitHub风格的Pull Request**
6. 最简单的方式是访问分支页面，然后点击"Create merge request"按钮

