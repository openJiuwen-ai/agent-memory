-- =====================================================
-- V9: 补充 kernel:restart 权限项（§1.5 RBAC 权限模型）
-- 2026-07-19 P0-3 v3: 日志中心 + 内核重启高危权限
-- 幂等：INSERT IGNORE，已建库升级时自动补齐
-- =====================================================

-- kernel:restart 权限：仅 SUPER_ADMIN / PLATFORM_ADMIN
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_060', 'SUPER_ADMIN', 'kernel:restart');
INSERT IGNORE INTO role_permissions (id, role, permission) VALUES ('rp_061', 'PLATFORM_ADMIN', 'kernel:restart');
