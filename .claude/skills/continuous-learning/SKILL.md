---
name: continuous-learning
description: 从 Claude Code 会话中自动提取可复用模式，保存为 learned skill。
origin: ECC
---

# 持续学习

会话结束时自动评估，提取值得复用的模式。

## 机制说明

持续学习有两种触发方式：

### 方式 1：自动触发（Stop Hook）

会话结束时，Stop Hook 调用 `evaluate-session.sh` 脚本：

```
会话结束
   │
   ▼
Stop Hook 触发 evaluate-session.sh
   │
   ├─ 记录会话时间戳到 .claude/sessions/（项目级目录）
   ├─ 检查会话长度（< 10 条消息则跳过）
   └─ 创建待评估标记到 .claude/sessions/pending-review/
         │
         ▼
下次会话 SessionStart 时
   │
   ▼
Claude 看到 pending-review 中的标记文件
   │
   ├─ 有值得提取的模式 → 生成 skill 到 .claude/skills/learned/（团队共享）
   └─ 没有 → 删除标记文件
```

注意：脚本本身不会"分析会话内容"（shell 脚本无法理解对话语义），
它的作用是创建标记，让下次会话的 Claude 来做分析和提取。

### 方式 2：手动触发（/learn 命令）

在会话中随时使用：

```
> /learn
> /learn pyright 报 import 错误时需要先检查 pyrightconfig.json
```

Claude 会立即分析当前会话，提取模式并生成 skill 文件。

## 配置 Hook

在 `settings.json` 的 hooks 中配置 Stop hook：

```json
{
  "Stop": [{
    "matcher": "*",
    "hooks": [{
      "type": "command",
      "command": ".claude/skills/continuous-learning/evaluate-session.sh",
      "async": true,
      "timeout": 5
    }]
  }]
}
```

安装脚本后需要确保执行权限：
```bash
chmod +x .claude/skills/continuous-learning/evaluate-session.sh
```

## 值得提取的模式

- 错误解决方案（特别是非显而易见的）
- 用户纠正过的行为（Claude 做错了，用户指出正确做法）
- 项目特定的 workaround
- 调试技巧
- 特定框架的使用模式

## 不值得提取的

- 简单的拼写错误修复
- 一次性的问题
- 外部 API 的临时问题

## 目录结构

```
.claude/（项目根目录下，随 git 团队共享）
├── sessions/
│   ├── 2026-03-10.tmp              # 会话时间记录
│   └── pending-review/             # 待评估标记
│       └── 2026-03-10-1430.md
└── skills/
    └── learned/                    # 提取出的 skill（团队共享）
        └── learned-pyright-venv.md
```

## 配置参数

通过环境变量控制：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MIN_SESSION_LENGTH` | 10 | 最少交互行数，低于此值不触发评估 |
