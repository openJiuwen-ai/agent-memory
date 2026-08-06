-- =====================================================
-- V11: Token 黑名单表（用于 JWT 登出失效）- GaussDB/PostgreSQL 版本
-- 2026-07-24: 实现 Token 黑名单机制，解决登出后 Token 仍有效的问题
-- =====================================================

CREATE TABLE IF NOT EXISTS token_blacklist (
    id          VARCHAR(64)  NOT NULL,           -- Token 的 jti（JWT ID）
    token       TEXT         NOT NULL,           -- 完整 Token（可选，用于调试）
    username    VARCHAR(64)  NOT NULL,           -- 用户名
    expires_at  TIMESTAMP    NOT NULL,           -- Token 过期时间
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
);

-- 索引：按过期时间清理
CREATE INDEX IF NOT EXISTS idx_blacklist_expires ON token_blacklist(expires_at);

-- 索引：按用户名查询（可选，用于强制下线某用户的所有 Token）
CREATE INDEX IF NOT EXISTS idx_blacklist_username ON token_blacklist(username);

-- 添加注释
COMMENT ON TABLE token_blacklist IS 'Token 黑名单表（用于 JWT 登出失效）';
COMMENT ON COLUMN token_blacklist.id IS 'Token 的 jti（JWT ID）';
COMMENT ON COLUMN token_blacklist.token IS '完整 Token（可选，用于调试）';
COMMENT ON COLUMN token_blacklist.username IS '用户名';
COMMENT ON COLUMN token_blacklist.expires_at IS 'Token 过期时间';
COMMENT ON COLUMN token_blacklist.created_at IS '创建时间';
