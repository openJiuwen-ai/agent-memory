# GitCode Committer 参考

## 配置

仓库信息从共享配置 `.claude/skills/gitcode-config.json` 读取（GitCode 系列 skill 共用，
`PRClient.from_config()` 默认加载它）。相关字段：

```json
{
  "upstream": {"owner": "<主仓 owner>", "repo": "<主仓 repo>", "base_branch": "<主分支>"}
}
```

不要硬编码具体工程名——值来自 `gitcode-config.json`。

## API 端点

| 操作 | 端点 |
|------|------|
| 获取 PR 详情 | `GET /repos/:owner/:repo/pulls/:number` |
| 获取 PR 文件 | `GET /repos/:owner/:repo/pulls/:number/files` |
| 获取 PR commits | `GET /repos/:owner/:repo/pulls/:number/commits` |
| 获取/创建 PR 评论 | `GET/POST /repos/:owner/:repo/pulls/:number/comments` |
| 更新 PR 设置 | `PATCH /repos/:owner/:repo/pulls/:number` |
| 获取关联 issue | `GET /repos/:owner/:repo/pulls/:number/issues` |
| 合并 PR | `PUT /repos/:owner/:repo/pulls/:number/merge` |
| 仓库通知 | `GET /repos/:owner/:repo/notifications?type=referer` |

认证：`PRIVATE-TOKEN` header。

## gitcode-approve 检查逻辑

1. `close_related_issue` 若为 true → 自动设为 false
2. 无关联 issue → 提醒提交人
3. Squash message 格式：`{MR标题}\n\nRefs: #{issue号}`

## 检视报告模板

```markdown
## PR #<N> 检视报告
### 基本信息
### 变更概览 (+/-)
### 检视意见
#### 🔴 必须修改 / 🟡 建议修改 / 🟢 良好实践
### 总体评价
```

## 检视策略

- 安全：硬编码凭证、输入验证、危险调用
- 质量：命名、注释、函数复杂度
- 测试：新代码有无测试覆盖
- 大型 PR 只分析核心文件
