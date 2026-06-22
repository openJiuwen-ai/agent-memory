# openlibing.com 登录指南

openlibing.com 需要 GitCode 账号登录才能查看 CodeCheck 报告。

## 登录方式

- 小程序登录（微信扫码）
- **短信登录**（推荐：适合 agent-browser 自动化）
- 密码登录

## agent-browser 短信登录流程

```bash
# 1. 打开页面
AGENT_BROWSER_DEFAULT_TIMEOUT=120000 agent-browser open "<report_url>"
agent-browser wait 10000

# 2. 点击"短信登录" tab
agent-browser click @e2   # ref 可能变化，先 snapshot 确认

# 3. 填写手机号
agent-browser type @e17 "1xxxxxxxxxx"

# 4. 勾选协议复选框 — 必须用 JS 触发事件，直接 click 无效！
agent-browser eval "
document.querySelectorAll('input[type=checkbox]').forEach(cb => {
  cb.checked = true;
  cb.dispatchEvent(new Event('change', {bubbles: true}));
  cb.dispatchEvent(new MouseEvent('click', {bubbles: true}));
});
"

# 5. 点击"获取验证码"
agent-browser click @e20  # "获取验证码" 所在的 clickable generic

# 6. 等待用户提供验证码，填入
agent-browser eval "document.querySelector('input[placeholder=\"请填写手机验证码\"]').value = '<code>'"

# 7. 点击登录
agent-browser click @e9

# 8. 等待跳转（至少 20 秒）
agent-browser wait 20000
agent-browser snapshot -c
```

## 关键陷阱

1. **复选框 click 无效**：页面使用 Vue/React 框架，`agent-browser click` 不触发框架事件。必须用 `agent-browser eval` + `dispatchEvent`。
2. **验证消息提示**：未正确勾选时显示 "请阅读并同意用户协议、隐私政策和数据共享"。
3. **验证码过期**：约 60 秒过期，过期需重新获取。
4. **URL 路径差异**：CI 评论中链接为 `entryCheckDashCode`，实际访问需改为 `entryCheckDash`。
5. **跳转等待**：登录后需等 20 秒以上让 SPA 完成跳转和渲染。
