-- 租户与用户管理模块数据库初始化脚本（MySQL 方言）
-- V6: 租户、用户、角色权限

-- 1. 租户表
CREATE TABLE IF NOT EXISTS tenants (
    id          VARCHAR(64)  NOT NULL,
    name        VARCHAR(128) NOT NULL,
    status      VARCHAR(32)  DEFAULT 'active',
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    remark      TEXT,
    scope_ids   TEXT,  -- 已分配的Scope ID列表（JSON数组）
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. 用户表
CREATE TABLE IF NOT EXISTS users (
    id          VARCHAR(64)  NOT NULL,
    tenant_id   VARCHAR(64),  -- 允许NULL，全局角色（PLATFORM_ADMIN、SECURITY_ADMIN）不绑定租户
    username    VARCHAR(128) NOT NULL,
    password    VARCHAR(256) NOT NULL,
    role        VARCHAR(32)  NOT NULL,
    scope_ids   TEXT,
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    remark      TEXT,
    PRIMARY KEY (id),
    UNIQUE KEY uk_users_username (username),  -- 仅username唯一
    CONSTRAINT fk_users_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_users_tenant_id ON users(tenant_id);
CREATE INDEX idx_users_role ON users(role);

-- 3. 角色权限表
CREATE TABLE IF NOT EXISTS role_permissions (
    id          VARCHAR(64) NOT NULL,
    role        VARCHAR(32) NOT NULL,
    permission  VARCHAR(64) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_role_permission (role, permission)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_role_permissions_role ON role_permissions(role);

-- 4. 种子数据：角色权限（使用 INSERT IGNORE 实现幂等）
-- SUPER_ADMIN（全量 20 项）
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_001', 'SUPER_ADMIN', 'tenant:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_002', 'SUPER_ADMIN', 'tenant:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_003', 'SUPER_ADMIN', 'user:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_004', 'SUPER_ADMIN', 'user:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_005', 'SUPER_ADMIN', 'memory:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_006', 'SUPER_ADMIN', 'memory:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_007', 'SUPER_ADMIN', 'memory:delete');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_008', 'SUPER_ADMIN', 'scope:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_009', 'SUPER_ADMIN', 'scope:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_010', 'SUPER_ADMIN', 'config:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_011', 'SUPER_ADMIN', 'config:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_012', 'SUPER_ADMIN', 'log:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_013', 'SUPER_ADMIN', 'ops:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_014', 'SUPER_ADMIN', 'ops:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_015', 'SUPER_ADMIN', 'audit:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_016', 'SUPER_ADMIN', 'governance:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_017', 'SUPER_ADMIN', 'governance:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_018', 'SUPER_ADMIN', 'trace:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_019', 'SUPER_ADMIN', 'template:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_020', 'SUPER_ADMIN', 'template:write');

-- PLATFORM_ADMIN（13 项）
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_021', 'PLATFORM_ADMIN', 'tenant:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_022', 'PLATFORM_ADMIN', 'user:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_023', 'PLATFORM_ADMIN', 'user:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_024', 'PLATFORM_ADMIN', 'memory:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_025', 'PLATFORM_ADMIN', 'scope:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_026', 'PLATFORM_ADMIN', 'config:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_027', 'PLATFORM_ADMIN', 'config:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_028', 'PLATFORM_ADMIN', 'log:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_029', 'PLATFORM_ADMIN', 'ops:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_030', 'PLATFORM_ADMIN', 'governance:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_031', 'PLATFORM_ADMIN', 'trace:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_032', 'PLATFORM_ADMIN', 'template:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_033', 'PLATFORM_ADMIN', 'template:write');

-- SECURITY_ADMIN（11 项）
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_034', 'SECURITY_ADMIN', 'tenant:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_035', 'SECURITY_ADMIN', 'user:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_036', 'SECURITY_ADMIN', 'memory:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_037', 'SECURITY_ADMIN', 'scope:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_038', 'SECURITY_ADMIN', 'config:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_039', 'SECURITY_ADMIN', 'log:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_040', 'SECURITY_ADMIN', 'ops:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_041', 'SECURITY_ADMIN', 'audit:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_042', 'SECURITY_ADMIN', 'governance:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_043', 'SECURITY_ADMIN', 'trace:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_044', 'SECURITY_ADMIN', 'template:read');

-- SCOPE_ADMIN（10 项）
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_045', 'SCOPE_ADMIN', 'memory:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_046', 'SCOPE_ADMIN', 'memory:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_047', 'SCOPE_ADMIN', 'memory:delete');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_048', 'SCOPE_ADMIN', 'scope:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_049', 'SCOPE_ADMIN', 'scope:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_050', 'SCOPE_ADMIN', 'ops:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_051', 'SCOPE_ADMIN', 'ops:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_052', 'SCOPE_ADMIN', 'governance:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_053', 'SCOPE_ADMIN', 'governance:write');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_054', 'SCOPE_ADMIN', 'trace:read');

-- READ_ONLY（7 项）
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_055', 'READ_ONLY', 'tenant:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_056', 'READ_ONLY', 'memory:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_057', 'READ_ONLY', 'scope:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_058', 'READ_ONLY', 'ops:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_059', 'READ_ONLY', 'governance:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_060', 'READ_ONLY', 'trace:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_063', 'READ_ONLY', 'log:read');

-- VIEWER（2 项）
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_061', 'VIEWER', 'memory:read');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_062', 'VIEWER', 'scope:read');

-- 5. 种子数据：默认租户（使用 ON DUPLICATE KEY UPDATE 实现幂等）
INSERT INTO tenants (id, name, status, remark)
VALUES ('tenant_default', '默认租户', 'active', '系统默认租户')
ON DUPLICATE KEY UPDATE id=id;

-- 6. 种子数据：默认超级管理员（用户名: admin, 密码: admin123）
INSERT INTO users (id, tenant_id, username, password, role, remark)
VALUES ('user_admin', 'tenant_default', 'admin', '$2a$10$hYy/XlvXudOwYSsbRbDruOEIkfoqoA5T4RoyEgq6GFmExMGsjtUpm', 'SUPER_ADMIN', '系统默认超级管理员（密码: admin123）')
ON DUPLICATE KEY UPDATE id=id;
