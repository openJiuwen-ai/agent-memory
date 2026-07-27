-- =====================================================
-- 租户与用户管理模块数据库初始化脚本（GaussDB/PostgreSQL）
-- V3: 租户、用户、角色权限
-- =====================================================

-- 1. 租户表
CREATE TABLE IF NOT EXISTS tenants (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    status VARCHAR(32) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    remark TEXT,
    scope_ids TEXT  -- 已分配的Scope ID列表（JSON数组）
);

-- 2. 用户表（租户内的用户）
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64),  -- 允许NULL，全局角色（PLATFORM_ADMIN、SECURITY_ADMIN）不绑定租户
    username VARCHAR(128) NOT NULL,
    password VARCHAR(256) NOT NULL,
    role VARCHAR(32) NOT NULL,
    scope_ids TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    remark TEXT,
    CONSTRAINT uk_users_username UNIQUE (username),  -- 仅username唯一
    CONSTRAINT fk_users_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- 3. 角色权限表
CREATE TABLE IF NOT EXISTS role_permissions (
    id VARCHAR(64) PRIMARY KEY,
    role VARCHAR(32) NOT NULL,
    permission VARCHAR(64) NOT NULL,
    CONSTRAINT uk_role_permission UNIQUE (role, permission)
);

-- 4. 插入默认角色权限数据（逐条插入，防止重复）
-- SUPER_ADMIN 权限（全量）
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_001', 'SUPER_ADMIN', 'tenant:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_001');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_002', 'SUPER_ADMIN', 'tenant:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_002');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_003', 'SUPER_ADMIN', 'user:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_003');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_004', 'SUPER_ADMIN', 'user:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_004');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_005', 'SUPER_ADMIN', 'memory:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_005');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_006', 'SUPER_ADMIN', 'memory:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_006');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_007', 'SUPER_ADMIN', 'memory:delete' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_007');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_008', 'SUPER_ADMIN', 'scope:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_008');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_009', 'SUPER_ADMIN', 'scope:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_009');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_010', 'SUPER_ADMIN', 'config:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_010');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_011', 'SUPER_ADMIN', 'config:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_011');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_012', 'SUPER_ADMIN', 'log:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_012');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_013', 'SUPER_ADMIN', 'ops:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_013');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_014', 'SUPER_ADMIN', 'ops:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_014');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_015', 'SUPER_ADMIN', 'audit:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_015');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_016', 'SUPER_ADMIN', 'governance:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_016');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_017', 'SUPER_ADMIN', 'governance:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_017');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_018', 'SUPER_ADMIN', 'trace:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_018');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_019', 'SUPER_ADMIN', 'template:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_019');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_020', 'SUPER_ADMIN', 'template:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_020');

-- PLATFORM_ADMIN 权限
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_021', 'PLATFORM_ADMIN', 'tenant:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_021');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_022', 'PLATFORM_ADMIN', 'user:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_022');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_023', 'PLATFORM_ADMIN', 'user:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_023');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_024', 'PLATFORM_ADMIN', 'memory:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_024');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_025', 'PLATFORM_ADMIN', 'scope:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_025');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_026', 'PLATFORM_ADMIN', 'config:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_026');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_027', 'PLATFORM_ADMIN', 'config:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_027');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_028', 'PLATFORM_ADMIN', 'log:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_028');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_029', 'PLATFORM_ADMIN', 'ops:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_029');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_030', 'PLATFORM_ADMIN', 'governance:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_030');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_031', 'PLATFORM_ADMIN', 'trace:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_031');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_032', 'PLATFORM_ADMIN', 'template:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_032');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_033', 'PLATFORM_ADMIN', 'template:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_033');

-- SECURITY_ADMIN 权限
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_034', 'SECURITY_ADMIN', 'tenant:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_034');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_035', 'SECURITY_ADMIN', 'user:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_035');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_036', 'SECURITY_ADMIN', 'memory:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_036');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_037', 'SECURITY_ADMIN', 'scope:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_037');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_038', 'SECURITY_ADMIN', 'config:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_038');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_039', 'SECURITY_ADMIN', 'log:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_039');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_040', 'SECURITY_ADMIN', 'ops:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_040');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_041', 'SECURITY_ADMIN', 'audit:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_041');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_042', 'SECURITY_ADMIN', 'governance:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_042');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_043', 'SECURITY_ADMIN', 'trace:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_043');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_044', 'SECURITY_ADMIN', 'template:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_044');

-- SCOPE_ADMIN 权限
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_045', 'SCOPE_ADMIN', 'memory:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_045');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_046', 'SCOPE_ADMIN', 'memory:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_046');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_047', 'SCOPE_ADMIN', 'memory:delete' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_047');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_048', 'SCOPE_ADMIN', 'scope:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_048');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_049', 'SCOPE_ADMIN', 'scope:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_049');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_050', 'SCOPE_ADMIN', 'ops:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_050');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_051', 'SCOPE_ADMIN', 'ops:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_051');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_052', 'SCOPE_ADMIN', 'governance:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_052');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_053', 'SCOPE_ADMIN', 'governance:write' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_053');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_054', 'SCOPE_ADMIN', 'trace:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_054');

-- READ_ONLY 权限
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_055', 'READ_ONLY', 'tenant:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_055');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_056', 'READ_ONLY', 'memory:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_056');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_057', 'READ_ONLY', 'scope:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_057');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_058', 'READ_ONLY', 'ops:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_058');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_059', 'READ_ONLY', 'governance:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_059');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_060', 'READ_ONLY', 'trace:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_060');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_063', 'READ_ONLY', 'log:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_063');

-- VIEWER 权限（只读查看记忆和Scope）
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_061', 'VIEWER', 'memory:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_061');
INSERT INTO role_permissions (id, role, permission) SELECT 'rp_062', 'VIEWER', 'scope:read' WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_062');

-- 5. 插入默认租户
INSERT INTO tenants (id, name, status, remark)
SELECT 'tenant_001', '默认租户', 'active', '系统默认租户'
WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE id = 'tenant_001');

INSERT INTO tenants (id, name, status, remark)
SELECT 'tenant_default', '平台租户', 'active', '平台管理租户'
WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE id = 'tenant_default');

-- 6. 插入默认超级管理员用户（用户名: admin, 密码: admin123）
-- BCrypt 加密后的 admin123: $2a$10$hYy/XlvXudOwYSsbRbDruOEIkfoqoA5T4RoyEgq6GFmExMGsjtUpm
INSERT INTO users (id, tenant_id, username, password, role, remark)
SELECT 'user_admin', 'tenant_default', 'admin', '$2a$10$hYy/XlvXudOwYSsbRbDruOEIkfoqoA5T4RoyEgq6GFmExMGsjtUpm', 'SUPER_ADMIN', '系统默认超级管理员（密码: admin123）'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin');

-- 7. 创建索引（如果不存在）
CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role);
