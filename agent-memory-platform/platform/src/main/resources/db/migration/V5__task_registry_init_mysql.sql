-- 运维中心任务注册表（功能8/9，MySQL 方言）：Dreaming 任务 + 迁移任务。IF NOT EXISTS 幂等。

CREATE TABLE IF NOT EXISTS task_registry (
    id              VARCHAR(64)  NOT NULL,
    admin_user_id   VARCHAR(64)  NOT NULL,
    task_type       VARCHAR(32)  NOT NULL,                 -- DREAMING / MIGRATION
    scope_id        VARCHAR(256),                           -- 任务关联的 scope_id
    user_id         VARCHAR(128),                           -- 任务关联的 user_id
    status          VARCHAR(16)  NOT NULL,                  -- pending/running/stopped/failed/completed
    task_config     TEXT,                                  -- JSON：任务配置
    task_result     TEXT,                                  -- JSON：任务结果
    error_message   TEXT,                                  -- 失败原因
    started_at      TIMESTAMP   NULL,                      -- 启动时间
    stopped_at      TIMESTAMP   NULL,                      -- 停止时间
    last_heartbeat  TIMESTAMP   NULL,                      -- 最近心跳时间
    created_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_task_admin_status (admin_user_id, status),
    KEY idx_task_type_status (task_type, status),
    KEY idx_task_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
