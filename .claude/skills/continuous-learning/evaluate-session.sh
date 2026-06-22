#!/bin/bash
# evaluate-session.sh — 会话结束时自动评估，提取可复用模式
#
# 工作原理：
#   1. 读取当天的会话记录
#   2. 检查会话长度是否达到阈值
#   3. 将会话摘要写入待评估文件，供下次会话时 Claude 分析提取
#
# 用法：由 Stop hook 自动调用，也可手动执行
#   .claude/skills/continuous-learning/evaluate-session.sh

set -euo pipefail

# === 配置 ===
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CLAUDE_DIR="${PROJECT_ROOT}/.claude"
SESSION_DIR="${CLAUDE_DIR}/sessions"
LEARNED_DIR="${CLAUDE_DIR}/skills/learned"
PENDING_DIR="${CLAUDE_DIR}/sessions/pending-review"
MIN_SESSION_LENGTH="${MIN_SESSION_LENGTH:-10}"
TODAY=$(date +%Y-%m-%d)
SESSION_FILE="${SESSION_DIR}/${TODAY}.tmp"

# === 确保目录存在 ===
mkdir -p "$SESSION_DIR" "$LEARNED_DIR" "$PENDING_DIR"

# === 记录会话时间戳 ===
if [ ! -f "$SESSION_FILE" ]; then
    cat > "$SESSION_FILE" <<EOF
# 会话记录 ${TODAY}

## 时间
开始: $(date +%H:%M)
EOF
else
    echo "更新: $(date +%H:%M)" >> "$SESSION_FILE"
fi

# === 检查会话长度 ===
# 通过会话文件的行数粗略判断交互量
if [ -f "$SESSION_FILE" ]; then
    line_count=$(wc -l < "$SESSION_FILE")
else
    line_count=0
fi

if [ "$line_count" -lt "$MIN_SESSION_LENGTH" ]; then
    # 会话太短，不值得评估
    exit 0
fi

# === 创建待评估标记 ===
# 在 pending-review 目录创建标记文件
# 下次 SessionStart 时 Claude 会看到这个标记，主动分析上次会话
PENDING_FILE="${PENDING_DIR}/${TODAY}-$(date +%H%M).md"

if [ ! -f "$PENDING_FILE" ]; then
    cat > "$PENDING_FILE" <<EOF
---
date: ${TODAY}
time: $(date +%H:%M)
status: pending
---

# 待评估会话

本次会话可能包含值得提取的模式。

## 评估指引

下次会话开始时，请检查以下内容：
- 是否有非显而易见的错误解决方案
- 是否有用户纠正过的行为模式
- 是否有项目特定的 workaround
- 是否有可复用的调试技巧

## 处理方式

如果发现值得提取的模式：
1. 创建 skill 文件到 ${LEARNED_DIR}/
2. 将此文件的 status 改为 extracted
3. 在 skill 文件中记录来源会话日期

如果没有值得提取的内容：
1. 将此文件的 status 改为 skipped
2. 或直接删除此文件
EOF

    echo "[学习] 会话已标记待评估: ${PENDING_FILE}" >&2
fi
