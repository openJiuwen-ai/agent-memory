import request from './request'
import type { UserRole } from '@/types/tenant'

/**
 * 获取用户列表
 * 调用后端 GET /api/v1/users
 */
export async function getUserList(): Promise<any[]> {
  const users: any[] = await request.get('/api/v1/users')
  // 转换后端字段名为前端期望的格式
  // 注意：不透传 password，避免密码哈希泄露到前端
  return users.map(u => ({
    id: u.id,
    username: u.username,
    role: u.role,
    tenantId: u.tenant_id,  // tenant_id -> tenantId
    // 兼容后端返回的 scope_ids 和前端的 scopeIds
    scopeIds: u.scope_ids ? JSON.parse(u.scope_ids) : (u.scopeIds ? JSON.parse(u.scopeIds) : []),
    remark: u.remark,
    createdAt: u.created_at,
    updatedAt: u.updated_at
  }))
}

/**
 * 创建用户
 * 调用后端 POST /api/v1/users
 */
export function createUser(data: {
  username: string
  password: string
  role: UserRole
  tenant_id?: string
  scopeIds?: string[]
  remark?: string
}): Promise<any> {
  return request.post('/api/v1/users', data, {
    headers: {
      'X-User-Role': 'SUPER_ADMIN',  // 当前用户角色
      'X-User-ScopeIds': '',  // 当前用户scope权限
    }
  })
}

/**
 * 更新用户
 * 调用后端 PUT /api/v1/users/{userId}
 */
export function updateUser(userId: string, data: {
  username?: string
  role?: UserRole
  remark?: string
  scopeIds?: string[]
  password?: string  // 添加密码字段
}): Promise<any> {
  return request.put(`/api/v1/users/${userId}`, data)
}

/**
 * 管理员重置用户密码（不需要原密码）
 * 调用后端 PUT /api/v1/users/{userId}/password
 */
export function resetUserPassword(userId: string, newPassword: string): Promise<void> {
  return request.put(`/api/v1/users/${userId}/password`, {
    newPassword,
  })
}

/**
 * 用户修改自己的密码（需要原密码）
 * 调用后端 POST /api/v1/users/password/change
 */
export function changeMyPassword(userId: string, oldPassword: string, newPassword: string): Promise<void> {
  return request.post('/api/v1/users/password/change', {
    oldPassword,
    newPassword,
  }, {
    headers: {
      'X-User-Id': userId,
    }
  })
}

/**
 * 删除用户
 * 调用后端 DELETE /api/v1/users/{userId}
 */
export function deleteUser(userId: string): Promise<void> {
  return request.delete(`/api/v1/users/${userId}`)
}
