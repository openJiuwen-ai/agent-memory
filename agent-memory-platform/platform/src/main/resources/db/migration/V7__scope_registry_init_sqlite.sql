-- =====================================================
-- Scope注册表初始化脚本（SQLite版本）
-- =====================================================

-- 1. 创建scope_registry表
CREATE TABLE IF NOT EXISTS scope_registry (
    id          VARCHAR(64) PRIMARY KEY,
    scope_id    VARCHAR(64) NOT NULL UNIQUE,
    scope_name  VARCHAR(128),
    description TEXT,
    scope_key   VARCHAR(256),
 	max_memories INTEGER DEFAULT 0,
    status      VARCHAR(32) DEFAULT 'unassigned',
    assigned_to_tenant_id VARCHAR(64),
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 为tenants表添加scope_ids字段（SQLite不支持ADD COLUMN IF NOT EXISTS，需要检查）
-- 注意：SQLite ALTER TABLE 功能有限，如果字段已存在会报错
-- 实际执行时需要确保该字段不存在，或使用迁移工具管理

-- 3. 创建索引
CREATE INDEX IF NOT EXISTS idx_scope_status ON scope_registry(status);
CREATE INDEX IF NOT EXISTS idx_scope_tenant ON scope_registry(assigned_to_tenant_id);
