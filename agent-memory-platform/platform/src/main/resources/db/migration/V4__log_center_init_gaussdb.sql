-- 日志中心表（功能4/5/6）：操作审计日志 + 用户消息日志。GaussDB / openGauss 方言，PG 兼容。
-- 参考 §6.3.1 操作审计日志表、§6.3.3 用户消息日志表。
-- 注意：运行日志不入库（§6.3.2），仅通过内核 HTTP 接口瞬时查询（/logs/tail + /logs/files + /logs/download）。

CREATE TABLE IF NOT EXISTS operation_logs (
    id              VARCHAR(64)  NOT NULL,
    admin_user_id   VARCHAR(64)  NOT NULL,
    operator_id     VARCHAR(64)  NOT NULL,
    operator_role   VARCHAR(32)  NOT NULL,
    operation_type  VARCHAR(32)  NOT NULL,
    target_type     VARCHAR(32)  NOT NULL,
    target_id       VARCHAR(256),
    target_name     VARCHAR(256),
    request_method  VARCHAR(8),
    request_path    VARCHAR(512),
    request_ip      VARCHAR(64),
    request_body    TEXT,
    response_status INT,
    error_message   TEXT,
    duration_ms     INT,
    operated_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
);
COMMENT ON TABLE operation_logs IS '操作审计日志表（服务层独有，内核不提供）';
CREATE INDEX IF NOT EXISTS idx_op_logs_admin_time ON operation_logs(admin_user_id, operated_at);
CREATE INDEX IF NOT EXISTS idx_op_logs_operator ON operation_logs(operator_id, operated_at);
CREATE INDEX IF NOT EXISTS idx_op_logs_type ON operation_logs(operation_type, operated_at);

CREATE TABLE IF NOT EXISTS request_response_logs (
    id              VARCHAR(64)  NOT NULL,
    request_id      VARCHAR(64)  NOT NULL,
    admin_user_id   VARCHAR(64)  NOT NULL,
    user_id         VARCHAR(64)  NOT NULL,
    scope_name      VARCHAR(128) NOT NULL,
    api_path        VARCHAR(256) NOT NULL,
    api_method      VARCHAR(8)   NOT NULL,
    message_count   INT,
    message_roles   TEXT,
    message_lengths TEXT,
    response_status INT          NOT NULL,
    response_time_ms INT         NOT NULL,
    memory_generated BOOLEAN,
    memory_count    INT,
    error_message   TEXT,
    client_ip       VARCHAR(64),
    user_agent      VARCHAR(256),
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE(request_id)
);
COMMENT ON TABLE request_response_logs IS 'L2 请求响应详情日志表（§6.3.3 双层架构 Layer 2，记录记忆业务API请求体+响应体）';
CREATE INDEX IF NOT EXISTS idx_rrl_admin_time ON request_response_logs(admin_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rrl_user ON request_response_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rrl_scope ON request_response_logs(scope_name, created_at);
