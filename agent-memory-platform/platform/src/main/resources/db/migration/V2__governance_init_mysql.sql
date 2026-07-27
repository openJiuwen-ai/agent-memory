-- 运维中心治理表（功能6，MySQL 方言）：策略 + 配额。IF NOT EXISTS 幂等。

CREATE TABLE IF NOT EXISTS governance_policies (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    admin_user_id   VARCHAR(64),                          -- NULL=全局策略
    policy_type     VARCHAR(32)  NOT NULL,                 -- LIFECYCLE/QUALITY/QUOTA/COMPLIANCE
    policy_name     VARCHAR(128) NOT NULL,
    policy_config   TEXT NOT NULL,                        -- JSON
    is_enabled      TINYINT(1)  DEFAULT 1,
    created_by      VARCHAR(64) NOT NULL,
    created_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_gov_policy_tenant_type (admin_user_id, policy_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tenant_quotas (
    id                          BIGINT       NOT NULL AUTO_INCREMENT,
    admin_user_id               VARCHAR(64)  NOT NULL,
    max_scopes                  INT          DEFAULT 100,
    max_users_per_scope         INT          DEFAULT 10000,
    max_memories_per_user       INT          DEFAULT 100000,
    max_messages_per_day        INT          DEFAULT 1000000,
    max_storage_mb              INT          DEFAULT 10240,
    current_scopes              INT          DEFAULT 0,
    current_storage_mb          DOUBLE       DEFAULT 0,
    updated_at                  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_tenant_quota (admin_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
