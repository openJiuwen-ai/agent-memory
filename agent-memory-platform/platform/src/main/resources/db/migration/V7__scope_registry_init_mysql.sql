-- =====================================================
-- Scope注册表初始化脚本（MySQL版本）
-- =====================================================

-- 1. 创建scope_registry表
CREATE TABLE IF NOT EXISTS scope_registry (
    id VARCHAR(64) PRIMARY KEY,
    scope_id VARCHAR(64) NOT NULL UNIQUE,
    scope_name VARCHAR(128),
    description TEXT COMMENT 'Scope描述',
    scope_key VARCHAR(256) COMMENT 'Scope Key（加密存储，仅注册时明文返回一次）',
    max_memories INTEGER DEFAULT 0 COMMENT '最大记忆数量配额（0=不限）',
    status VARCHAR(32) DEFAULT 'unassigned',
    assigned_to_tenant_id VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_scope_tenant FOREIGN KEY (assigned_to_tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Scope注册表';

-- 2. 为tenants表添加scope_ids字段
ALTER TABLE tenants ADD COLUMN scope_ids TEXT COMMENT '已分配的Scope ID列表（JSON数组）';

-- 3. 创建索引
CREATE INDEX idx_scope_status ON scope_registry(status);
CREATE INDEX idx_scope_tenant ON scope_registry(assigned_to_tenant_id);
