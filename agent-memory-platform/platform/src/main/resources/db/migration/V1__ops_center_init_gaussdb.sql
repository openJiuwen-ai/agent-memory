-- 运维中心本地持久化表（GaussDB / openGauss 方言，PG 兼容，生产用）
-- 引擎侧记忆数据由 :8516 管理，本模块不直接读写引擎 DB。
-- openGauss 支持 CREATE INDEX IF NOT EXISTS，靠 IF NOT EXISTS 保证幂等。

CREATE TABLE IF NOT EXISTS feature_flag (
    id                      BIGSERIAL    NOT NULL,
    tenant_id               VARCHAR(32)  NOT NULL,
    scope_id                VARCHAR(94)  NOT NULL,                 -- '__default__' 为默认 profile
    enable_long_term_mem   BOOLEAN      DEFAULT TRUE,
    enable_user_profile    BOOLEAN      DEFAULT TRUE,
    enable_semantic_memory BOOLEAN      DEFAULT TRUE,
    enable_episodic_memory BOOLEAN      DEFAULT TRUE,
    enable_summary_memory  BOOLEAN      DEFAULT TRUE,
    custom_params           TEXT,                                  -- JSON
    enabled                 BOOLEAN      DEFAULT TRUE,
    priority                INT          DEFAULT 100,
    created_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_by              VARCHAR(64),
    PRIMARY KEY (id),
    UNIQUE (tenant_id, scope_id)
);

CREATE TABLE IF NOT EXISTS ops_parameter (
    id              BIGSERIAL    NOT NULL,
    tenant_id       VARCHAR(32)  NOT NULL,
    scope_id        VARCHAR(94),
    param_key       VARCHAR(128) NOT NULL,
    param_value     TEXT,
    param_type      VARCHAR(32),                          -- engine/scope/agent/bootstrap/retrieval/dreaming
    value_json      TEXT,
    is_draft        BOOLEAN      DEFAULT TRUE,
    updated_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_by      VARCHAR(64),
    PRIMARY KEY (id),
    UNIQUE (tenant_id, scope_id, param_key)
);

CREATE TABLE IF NOT EXISTS ops_command_catalog (
    command_code    VARCHAR(64)  NOT NULL,
    command_name    VARCHAR(128) NOT NULL,
    category        VARCHAR(32),                          -- inspection/admin/task/maintenance
    backend_action  VARCHAR(128),
    enabled         BOOLEAN      DEFAULT TRUE,
    gap_reason      VARCHAR(256),
    require_confirm BOOLEAN      DEFAULT FALSE,
    description     TEXT,
    PRIMARY KEY (command_code)
);

CREATE TABLE IF NOT EXISTS command_execution_log (
    execution_id     VARCHAR(64)  NOT NULL,
    command_code      VARCHAR(64)  NOT NULL,
    tenant_id        VARCHAR(32),
    scope_id         VARCHAR(94),
    user_id          VARCHAR(128),
    payload_snapshot TEXT,
    result_snapshot  TEXT,
    status           VARCHAR(16)  NOT NULL,               -- success/failed/gap/dry_run
    gap_hint         VARCHAR(256),
    duration_ms      INT,
    operator_id      VARCHAR(64),
    request_ip       VARCHAR(64),
    reason           VARCHAR(256),
    created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (execution_id)
);
CREATE INDEX IF NOT EXISTS idx_exec_command       ON command_execution_log(command_code);
CREATE INDEX IF NOT EXISTS idx_exec_tenant_time   ON command_execution_log(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS memory_change_log_snapshot (
    id                  BIGSERIAL    NOT NULL,
    mem_id              VARCHAR(128),                 -- CREATE/按scope删除时无单一mem_id，可空
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
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_change_mem         ON memory_change_log_snapshot(mem_id);
CREATE INDEX IF NOT EXISTS idx_change_tenant_time ON memory_change_log_snapshot(tenant_id, created_at);

-- 命令目录种子数据由 OpsCommandCatalogSeed (CommandLineRunner) 幂等写入，跨 DB 通用。
