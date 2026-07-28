-- 运维中心本地持久化表（MySQL 方言，生产用）
-- 索引内联进 CREATE TABLE，靠 IF NOT EXISTS 保证幂等（MySQL 不支持 CREATE INDEX IF NOT EXISTS）。
-- 引擎侧记忆数据由 :8516 管理，本模块不直接读写引擎 DB。

CREATE TABLE IF NOT EXISTS feature_flag (
    id                      BIGINT       NOT NULL AUTO_INCREMENT,
    tenant_id               VARCHAR(32)  NOT NULL,
    scope_id                VARCHAR(94)  NOT NULL,                 -- '__default__' 为默认 profile
    enable_long_term_mem   TINYINT(1)   DEFAULT 1,
    enable_user_profile    TINYINT(1)   DEFAULT 1,
    enable_semantic_memory TINYINT(1)   DEFAULT 1,
    enable_episodic_memory TINYINT(1)   DEFAULT 1,
    enable_summary_memory  TINYINT(1)   DEFAULT 1,
    custom_params           TEXT,                                  -- JSON
    enabled                 TINYINT(1)   DEFAULT 1,
    priority                INT          DEFAULT 100,
    created_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by              VARCHAR(64),
    PRIMARY KEY (id),
    UNIQUE KEY uk_feature_flag (tenant_id, scope_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ops_parameter (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    tenant_id       VARCHAR(32)  NOT NULL,
    scope_id        VARCHAR(94),
    param_key       VARCHAR(128) NOT NULL,
    param_value     TEXT,
    param_type      VARCHAR(32),                          -- engine/scope/agent/bootstrap/retrieval/dreaming
    value_json      TEXT,
    is_draft        TINYINT(1)  DEFAULT 1,
    updated_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by      VARCHAR(64),
    PRIMARY KEY (id),
    UNIQUE KEY uk_ops_param (tenant_id, scope_id, param_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ops_command_catalog (
    command_code    VARCHAR(64)  NOT NULL,
    command_name    VARCHAR(128) NOT NULL,
    category        VARCHAR(32),                          -- inspection/admin/task/maintenance
    backend_action  VARCHAR(128),
    enabled         TINYINT(1)   DEFAULT 1,
    gap_reason      VARCHAR(256),
    require_confirm TINYINT(1)   DEFAULT 0,
    description     TEXT,
    PRIMARY KEY (command_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS command_execution_log (
    execution_id     VARCHAR(64)  NOT NULL,
    command_code      VARCHAR(64)  NOT NULL,
    tenant_id        VARCHAR(32),
    scope_id         VARCHAR(94),
    user_id          VARCHAR(128),
    payload_snapshot MEDIUMTEXT,
    result_snapshot  MEDIUMTEXT,
    status           VARCHAR(16)  NOT NULL,               -- success/failed/gap/dry_run
    gap_hint         VARCHAR(256),
    duration_ms      INT,
    operator_id      VARCHAR(64),
    request_ip       VARCHAR(64),
    reason           VARCHAR(256),
    created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (execution_id),
    KEY idx_exec_command (command_code),
    KEY idx_exec_tenant_time (tenant_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS memory_change_log_snapshot (
    id                  BIGINT       NOT NULL AUTO_INCREMENT,
    mem_id              VARCHAR(128),                    -- CREATE/按scope删除时无单一mem_id，可空
    tenant_id           VARCHAR(32) NOT NULL,
    scope_id            VARCHAR(94),
    user_id             VARCHAR(128),
    change_type         VARCHAR(16) NOT NULL,             -- CREATE/UPDATE/DELETE
    old_content         TEXT,
    new_content         TEXT,
    operator_id         VARCHAR(64),
    request_ip          VARCHAR(64),
    reason              VARCHAR(256),
    source_execution_id VARCHAR(64),
    created_at          TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_change_mem (mem_id),
    KEY idx_change_tenant_time (tenant_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 命令目录种子数据由 OpsCommandCatalogSeed (CommandLineRunner) 幂等写入，跨 DB 通用。
