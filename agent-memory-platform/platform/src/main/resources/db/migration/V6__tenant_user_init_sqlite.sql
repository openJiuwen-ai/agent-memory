-- 认证中心表（SQLite 兼容版，无 COMMENT / FK 语法限制）
-- 对应 schema.sql 中 GaussDB 的 tenants/users/role_permissions 建表 + 种子数据

-- 1. 租户表
CREATE TABLE IF NOT EXISTS tenants (
    id          VARCHAR(64) PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    status      VARCHAR(32) DEFAULT 'active',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    remark      TEXT,
    scope_ids   TEXT  -- 已分配的Scope ID列表（JSON数组）
);

-- 2. 用户表
CREATE TABLE IF NOT EXISTS users (
    id          VARCHAR(64) PRIMARY KEY,
    tenant_id   VARCHAR(64),  -- 允许NULL，全局角色（PLATFORM_ADMIN、SECURITY_ADMIN）不绑定租户
    username    VARCHAR(128) NOT NULL,
    password    VARCHAR(256) NOT NULL,
    role        VARCHAR(32) NOT NULL,
    scope_ids   TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    remark      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);  -- 仅username唯一

-- 3. 角色权限表
CREATE TABLE IF NOT EXISTS role_permissions (
    id          VARCHAR(64) PRIMARY KEY,
    role        VARCHAR(32) NOT NULL,
    permission  VARCHAR(64) NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_role_permission ON role_permissions(role, permission);

-- 4. 种子数据：角色权限
-- SUPER_ADMIN（全量 20 项）
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_001', 'SUPER_ADMIN', 'tenant:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_002', 'SUPER_ADMIN', 'tenant:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_003', 'SUPER_ADMIN', 'user:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_004', 'SUPER_ADMIN', 'user:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_005', 'SUPER_ADMIN', 'memory:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_006', 'SUPER_ADMIN', 'memory:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_007', 'SUPER_ADMIN', 'memory:delete');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_008', 'SUPER_ADMIN', 'scope:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_009', 'SUPER_ADMIN', 'scope:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_010', 'SUPER_ADMIN', 'config:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_011', 'SUPER_ADMIN', 'config:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_012', 'SUPER_ADMIN', 'log:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_013', 'SUPER_ADMIN', 'ops:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_014', 'SUPER_ADMIN', 'ops:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_015', 'SUPER_ADMIN', 'audit:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_016', 'SUPER_ADMIN', 'governance:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_017', 'SUPER_ADMIN', 'governance:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_018', 'SUPER_ADMIN', 'trace:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_019', 'SUPER_ADMIN', 'template:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_020', 'SUPER_ADMIN', 'template:write');

-- PLATFORM_ADMIN（13 项）
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_021', 'PLATFORM_ADMIN', 'tenant:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_022', 'PLATFORM_ADMIN', 'user:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_023', 'PLATFORM_ADMIN', 'user:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_024', 'PLATFORM_ADMIN', 'memory:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_025', 'PLATFORM_ADMIN', 'scope:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_026', 'PLATFORM_ADMIN', 'config:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_027', 'PLATFORM_ADMIN', 'config:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_028', 'PLATFORM_ADMIN', 'log:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_029', 'PLATFORM_ADMIN', 'ops:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_030', 'PLATFORM_ADMIN', 'governance:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_031', 'PLATFORM_ADMIN', 'trace:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_032', 'PLATFORM_ADMIN', 'template:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_033', 'PLATFORM_ADMIN', 'template:write');

-- SECURITY_ADMIN（11 项）
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_034', 'SECURITY_ADMIN', 'tenant:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_035', 'SECURITY_ADMIN', 'user:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_036', 'SECURITY_ADMIN', 'memory:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_037', 'SECURITY_ADMIN', 'scope:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_038', 'SECURITY_ADMIN', 'config:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_039', 'SECURITY_ADMIN', 'log:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_040', 'SECURITY_ADMIN', 'ops:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_041', 'SECURITY_ADMIN', 'audit:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_042', 'SECURITY_ADMIN', 'governance:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_043', 'SECURITY_ADMIN', 'trace:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_044', 'SECURITY_ADMIN', 'template:read');

-- SCOPE_ADMIN（13 项）
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_045', 'SCOPE_ADMIN', 'memory:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_046', 'SCOPE_ADMIN', 'memory:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_047', 'SCOPE_ADMIN', 'memory:delete');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_048', 'SCOPE_ADMIN', 'scope:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_049', 'SCOPE_ADMIN', 'scope:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_050', 'SCOPE_ADMIN', 'ops:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_051', 'SCOPE_ADMIN', 'ops:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_052', 'SCOPE_ADMIN', 'governance:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_053', 'SCOPE_ADMIN', 'governance:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_054', 'SCOPE_ADMIN', 'trace:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_064', 'SCOPE_ADMIN', 'config:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_065', 'SCOPE_ADMIN', 'config:write');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_066', 'SCOPE_ADMIN', 'log:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_067', 'SCOPE_ADMIN', 'tenant:read');

-- READ_ONLY（7 项）
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_055', 'READ_ONLY', 'tenant:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_056', 'READ_ONLY', 'memory:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_057', 'READ_ONLY', 'scope:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_058', 'READ_ONLY', 'ops:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_059', 'READ_ONLY', 'governance:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_060', 'READ_ONLY', 'trace:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_063', 'READ_ONLY', 'log:read');

-- VIEWER（2 项）
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_061', 'VIEWER', 'memory:read');
INSERT OR IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_062', 'VIEWER', 'scope:read');

-- 5. 种子数据：默认租户
INSERT OR IGNORE INTO tenants (id, name, status, remark) VALUES ('tenant_default', '默认租户', 'active', '系统默认租户');

-- 6. 种子数据：默认超级管理员（用户名: admin, 密码: admin123）
INSERT OR IGNORE INTO users (id, tenant_id, username, password, role, remark)
VALUES ('user_admin', 'tenant_default', 'admin', '$2a$10$hYy/XlvXudOwYSsbRbDruOEIkfoqoA5T4RoyEgq6GFmExMGsjtUpm', 'SUPER_ADMIN', '系统默认超级管理员（密码: admin123）');

-- 7. 索引
CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role);
