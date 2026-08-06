-- =====================================================
-- V10: 日志一键采集记录表（§6.4.4）
-- 2026-07-19: 一键采集只分场景+时间，采集完生成记录，用户从列表下载
-- 命名规则：场景-时间戳-UUID
-- =====================================================

CREATE TABLE IF NOT EXISTS log_collect_records (
    id              VARCHAR(64)  PRIMARY KEY,              -- UUID
    scene           VARCHAR(64)  NOT NULL,                 -- 采集场景：故障排查/日常巡检/性能诊断/其他
    name            VARCHAR(256) NOT NULL,                 -- 采集包名称：场景-时间戳-UUID
    start_date      VARCHAR(32)  NOT NULL,                 -- 采集开始日期
    end_date        VARCHAR(32)  NOT NULL,                 -- 采集结束日期
    tenant_id       VARCHAR(64),                           -- 租户ID（可空，default）
    file_path       TEXT         NOT NULL,                 -- 采集包磁盘路径
    file_size       BIGINT,                                -- 采集包大小（字节）
    status          VARCHAR(32)  DEFAULT 'READY',          -- READY / FAILED
    operator_id     VARCHAR(64),                           -- 操作人
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 创建时间
    remark          TEXT                                   -- 备注
);

COMMENT ON TABLE log_collect_records IS '日志一键采集记录表';
COMMENT ON COLUMN log_collect_records.id IS '主键（UUID）';
COMMENT ON COLUMN log_collect_records.scene IS '采集场景';
COMMENT ON COLUMN log_collect_records.name IS '采集包名称';
COMMENT ON COLUMN log_collect_records.start_date IS '采集开始日期';
COMMENT ON COLUMN log_collect_records.end_date IS '采集结束日期';
COMMENT ON COLUMN log_collect_records.tenant_id IS '租户ID';
COMMENT ON COLUMN log_collect_records.file_path IS '采集包磁盘路径';
COMMENT ON COLUMN log_collect_records.file_size IS '采集包大小（字节）';
COMMENT ON COLUMN log_collect_records.status IS '状态：READY / FAILED';
COMMENT ON COLUMN log_collect_records.operator_id IS '操作人';
COMMENT ON COLUMN log_collect_records.created_at IS '创建时间';
COMMENT ON COLUMN log_collect_records.remark IS '备注';

CREATE INDEX IF NOT EXISTS idx_log_collect_created_at ON log_collect_records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_collect_scene ON log_collect_records(scene);
