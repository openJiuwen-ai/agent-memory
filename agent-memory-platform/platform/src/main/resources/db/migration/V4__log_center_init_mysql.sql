-- 日志中心表（功能4/5/6）：操作审计日志 + 用户消息日志。MySQL 方言。
-- 参考 §6.3.1 操作审计日志表、§6.3.3 用户消息日志表。
-- 注意：运行日志不入库（§6.3.2），仅通过内核 HTTP 接口瞬时查询（/logs/tail + /logs/files + /logs/download）。

CREATE TABLE IF NOT EXISTS operation_logs (
    id              VARCHAR(64)  NOT NULL,                -- UUID字符串
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
    request_body    LONGTEXT,
    response_status INT,
    error_message   TEXT,
    duration_ms     INT,
    operated_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_op_logs_admin_time (admin_user_id, operated_at),
    INDEX idx_op_logs_operator (operator_id, operated_at),
    INDEX idx_op_logs_type (operation_type, operated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS request_response_logs (
    id              VARCHAR(64)  NOT NULL,
    request_id      VARCHAR(64)  NOT NULL,
    admin_user_id   VARCHAR(64)  NOT NULL,
    user_id         VARCHAR(64)  NOT NULL,
    scope_name      VARCHAR(128) NOT NULL,
    api_path        VARCHAR(256) NOT NULL,
    api_method      VARCHAR(8)   NOT NULL,
    message_count   INT,
    message_roles   LONGTEXT,
    message_lengths LONGTEXT,
    response_status INT          NOT NULL,
    response_time_ms INT         NOT NULL,
    memory_generated TINYINT(1),
    memory_count    INT,
    error_message   TEXT,
    client_ip       VARCHAR(64),
    user_agent      VARCHAR(256),
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE(request_id),
    INDEX idx_rrl_admin_time (admin_user_id, created_at),
    INDEX idx_rrl_user (user_id, created_at),
    INDEX idx_rrl_scope (scope_name, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
