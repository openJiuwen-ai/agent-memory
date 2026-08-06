-- =====================================================
-- V11: Token 黑名单表（用于 JWT 登出失效）- MySQL 版本
-- 2026-07-24: 实现 Token 黑名单机制，解决登出后 Token 仍有效的问题
-- =====================================================

CREATE TABLE IF NOT EXISTS token_blacklist (
    id          VARCHAR(64)  NOT NULL,           -- Token 的 jti（JWT ID）
    token       TEXT         NOT NULL,           -- 完整 Token（可选，用于调试）
    username    VARCHAR(64)  NOT NULL,           -- 用户名
    expires_at  TIMESTAMP    NOT NULL,           -- Token 过期时间
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_blacklist_expires (expires_at),
    INDEX idx_blacklist_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
