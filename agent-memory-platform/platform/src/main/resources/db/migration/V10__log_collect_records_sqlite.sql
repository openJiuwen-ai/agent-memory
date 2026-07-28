-- =====================================================
-- V8: 日志一键采集记录表（§6.4.4）
-- 2026-07-19: 一键采集只分场景+时间，采集完生成记录，用户从列表下载
-- 命名规则：场景-时间戳-UUID
-- =====================================================

CREATE TABLE IF NOT EXISTS log_collect_records (
    id TEXT PRIMARY KEY,                  -- UUID
    scene TEXT NOT NULL,                  -- 采集场景：故障排查/日常巡检/性能诊断/其他
    name TEXT NOT NULL,                   -- 采集包名称：场景-时间戳-UUID
    start_date TEXT NOT NULL,             -- 采集开始日期
    end_date TEXT NOT NULL,               -- 采集结束日期
    tenant_id TEXT,                       -- 租户ID（可空，default）
    file_path TEXT NOT NULL,              -- 采集包磁盘路径
    file_size BIGINT,                     -- 采集包大小（字节）
    status TEXT DEFAULT 'READY',          -- READY / FAILED
    operator_id TEXT,                     -- 操作人
    created_at TEXT NOT NULL,             -- 创建时间
    remark TEXT                           -- 备注
);

CREATE INDEX IF NOT EXISTS idx_log_collect_created_at ON log_collect_records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_collect_scene ON log_collect_records(scene);
