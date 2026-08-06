import type { Permission } from '@/types/permission'
import type { UserRole } from '@/types/tenant'

/**
 * 角色-权限映射表（基于 V3-记忆系统服务化设计-v4.0 §权限模型）
 *
 * 6 角色 × 17 权限矩阵：
 * | 权限 | SUPER_ADMIN | PLATFORM_ADMIN | SECURITY_ADMIN | SCOPE_ADMIN | READ_ONLY | VIEWER |
 * |---|---|---|---|---|---|---|
 * | tenant:read | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
 * | tenant:write | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
 * | user:read | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
 * | user:write | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
 * | config:read | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
 * | config:write | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ |
 * | ops:read | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
 * | ops:write | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |
 * | memory:read | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
 * | memory:write | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |
 * | memory:delete | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |
 * | log:read | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
 * | trace:read | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
 * | template:read | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
 * | template:write | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
 * | scope:read | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
 * | scope:write | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |
 */

export const ROLE_PERMISSIONS: Record<UserRole, Permission[]> = {
  SUPER_ADMIN: [
    'tenant:read', 'tenant:write',
    'user:read', 'user:write',
    'config:read', 'config:write',
    'ops:read', 'ops:write',
    'memory:read', 'memory:write', 'memory:delete',
    'log:read',
    'trace:read',
    'template:read', 'template:write',
    'scope:read', 'scope:write',
  ],
  PLATFORM_ADMIN: [
    'tenant:read',
    'user:read', 'user:write',
    'config:read', 'config:write',
    'ops:read',
    'memory:read',
    'log:read',
    'trace:read',
    'template:read', 'template:write',
    'scope:read',
  ],
  SECURITY_ADMIN: [
    'tenant:read',
    'user:read',
    'config:read',
    'ops:read',
    'memory:read',
    'log:read',
    'trace:read',
    'template:read',
    'scope:read',
  ],
  SCOPE_ADMIN: [
    'tenant:read',
    'config:read', 'config:write',
    'ops:read', 'ops:write',
    'memory:read', 'memory:write', 'memory:delete',
    'log:read',
    'trace:read',
    'scope:read', 'scope:write',
  ],
  READ_ONLY: [
    'tenant:read',
    'ops:read',
    'memory:read',
    'log:read',
    'trace:read',
    'scope:read',
  ],
  VIEWER: [
    'memory:read',
    'scope:read',
  ],
}

/**
 * 获取指定角色的权限列表
 */
export function getPermissionsByRole(role: UserRole): Permission[] {
  return ROLE_PERMISSIONS[role] || []
}

/**
 * 检查角色是否拥有指定权限
 */
export function hasPermission(role: UserRole, permission: Permission): boolean {
  return ROLE_PERMISSIONS[role]?.includes(permission) ?? false
}
