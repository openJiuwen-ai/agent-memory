-- =====================================================
-- Scope注册表初始化脚本（GaussDB/PostgreSQL版本）
-- =====================================================

-- 1. 创建scope_registry表
CREATE TABLE IF NOT EXISTS scope_registry (
    id VARCHAR(64) PRIMARY KEY,
    scope_id VARCHAR(64) NOT NULL UNIQUE,
    scope_name VARCHAR(128),
    description TEXT,
    status VARCHAR(32) DEFAULT 'unassigned',
    assigned_to_tenant_id VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_scope_tenant FOREIGN KEY (assigned_to_tenant_id) REFERENCES tenants(id)
);

-- 2. 为tenants表添加scope_ids字段
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS scope_ids TEXT;
COMMENT ON COLUMN tenants.scope_ids IS '已分配的Scope ID列表（JSON数组）';

-- 3. 添加注释
COMMENT ON TABLE scope_registry IS 'Scope注册表';
COMMENT ON COLUMN scope_registry.id IS '主键（UUID）';
COMMENT ON COLUMN scope_registry.scope_id IS 'Scope ID（全局唯一）';
COMMENT ON COLUMN scope_registry.scope_name IS 'Scope名称';
COMMENT ON COLUMN scope_registry.description IS 'Scope描述';
COMMENT ON COLUMN scope_registry.status IS '状态：unassigned/assigned';
COMMENT ON COLUMN scope_registry.assigned_to_tenant_id IS '分配给的租户ID';

-- 4. 创建索引
CREATE INDEX IF NOT EXISTS idx_scope_status ON scope_registry(status);
CREATE INDEX IF NOT EXISTS idx_scope_tenant ON scope_registry(assigned_to_tenant_id);
