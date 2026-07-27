-- 日志中心表（功能4/5/6）：操作审计日志 + 用户消息日志。IF NOT EXISTS 幂等。
-- 参考 §6.3.1 操作审计日志表、§6.3.3 用户消息日志表。
-- 注意：运行日志不入库（§6.3.2），仅通过内核 HTTP 接口瞬时查询（/logs/tail + /logs/files + /logs/download）。

-- 操作审计日志表（服务层独有，内核不提供）
CREATE TABLE IF NOT EXISTS operation_logs (
    id              TEXT PRIMARY KEY,                -- UUID字符串（应用层生成）
    admin_user_id   VARCHAR(64) NOT NULL,
    operator_id     VARCHAR(64) NOT NULL,            -- 操作人
    operator_role   VARCHAR(32) NOT NULL,            -- 操作人角色
    operation_type  VARCHAR(32) NOT NULL,            -- 操作类型: CONFIG_UPDATE/MEMORY_ADD/...
    target_type     VARCHAR(32) NOT NULL,            -- 目标对象类型: SCOPE/MEMORY/VARIABLE/...
    target_id       VARCHAR(256),                    -- 目标对象ID
    target_name     VARCHAR(256),                    -- 目标对象名称
    request_method  VARCHAR(8),                      -- HTTP方法
    request_path    VARCHAR(512),                    -- 请求路径
    request_ip      VARCHAR(64),                     -- 请求IP
    request_body    TEXT,                            -- 请求参数(脱敏, JSON字符串)
    response_status INTEGER,                         -- 响应状态码
    error_message   TEXT,                            -- 错误信息
    duration_ms     INTEGER,                         -- 耗时(毫秒)
    operated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_op_logs_admin_time ON operation_logs(admin_user_id, operated_at);
CREATE INDEX IF NOT EXISTS idx_op_logs_operator ON operation_logs(operator_id, operated_at);
CREATE INDEX IF NOT EXISTS idx_op_logs_type ON operation_logs(operation_type, operated_at);

-- 用户消息日志表（L2 请求响应详情日志，§6.3.3 双层架构 Layer 2，记录记忆业务API请求体+响应体）
CREATE TABLE IF NOT EXISTS request_response_logs (
    id              TEXT PRIMARY KEY,                -- UUID字符串
    request_id      VARCHAR(64) NOT NULL UNIQUE,    -- 请求唯一ID
    admin_user_id   VARCHAR(64) NOT NULL,
    user_id         VARCHAR(64) NOT NULL,
    scope_name      VARCHAR(128) NOT NULL,
    api_path        VARCHAR(256) NOT NULL,           -- API路径
    api_method      VARCHAR(8) NOT NULL,             -- HTTP方法
    message_count   INTEGER,                         -- 消息数量(add_messages时)
    message_roles   TEXT,                            -- 消息角色列表(JSON字符串)
    message_lengths TEXT,                            -- 各消息长度(JSON字符串)
    response_status INTEGER NOT NULL,                -- 响应状态码
    response_time_ms INTEGER NOT NULL,               -- 响应耗时
    memory_generated INTEGER,                        -- 0=否 1=是
    memory_count    INTEGER,                         -- 生成记忆数量
    error_message   TEXT,                            -- 错误信息
    client_ip       VARCHAR(64),                    -- 客户端IP
    user_agent      VARCHAR(256),                    -- User-Agent
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rrl_admin_time ON request_response_logs(admin_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rrl_user ON request_response_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rrl_scope ON request_response_logs(scope_name, created_at);
