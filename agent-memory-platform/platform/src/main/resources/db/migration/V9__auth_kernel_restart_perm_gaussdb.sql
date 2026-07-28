-- =====================================================
-- V9: 补充 kernel:restart 权限项（§1.5 RBAC 权限模型）
-- 2026-07-19 P0-3 v3: 日志中心 + 内核重启高危权限
-- 幂等：INSERT ... WHERE NOT EXISTS，已建库升级时自动补齐
-- =====================================================

-- kernel:restart 权限：仅 SUPER_ADMIN / PLATFORM_ADMIN
INSERT INTO role_permissions (id, role, permission)
SELECT 'rp_060', 'SUPER_ADMIN', 'kernel:restart'
WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_060');

INSERT INTO role_permissions (id, role, permission)
SELECT 'rp_061', 'PLATFORM_ADMIN', 'kernel:restart'
WHERE NOT EXISTS (SELECT 1 FROM role_permissions WHERE id = 'rp_061');
