import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as loginApi, logout as logoutApi } from '@/api/auth'
import type { UserRole } from '@/types/tenant'
import type { Permission } from '@/types/permission'
import { getPermissionsByRole } from '@/api/permissions'

export const useUserStore = defineStore('user', () => {
  // ---- state ----
  const token = ref<string>(localStorage.getItem('token') || '')
  const username = ref<string>(localStorage.getItem('username') || '')
  const role = ref<UserRole | ''>((localStorage.getItem('role') as UserRole) || '')
  const tenantId = ref<string>(localStorage.getItem('tenantId') || '')
  const scopeIds = ref<string[]>(JSON.parse(localStorage.getItem('scopeIds') || '[]'))
  const permissions = ref<Permission[]>(
    (() => {
      const stored = localStorage.getItem('permissions')
      if (stored) return JSON.parse(stored)
      // 如果没有存储的权限，根据角色初始化
      const role = localStorage.getItem('role') as UserRole
      return role ? getPermissionsByRole(role) : []
    })()
  )

  // ---- getters ----（对齐后端 V6 种子角色名：SUPER_ADMIN/PLATFORM_ADMIN/SECURITY_ADMIN/SCOPE_ADMIN/READ_ONLY）
  const isLoggedIn = computed(() => !!token.value)
  const isSuperAdmin = computed(() => (role.value as string) === 'SUPER_ADMIN')
  const isPlatformAdmin = computed(() => (role.value as string) === 'PLATFORM_ADMIN')
  const isSecurityAdmin = computed(() => (role.value as string) === 'SECURITY_ADMIN')
  const isScopeAdmin = computed(() => (role.value as string) === 'SCOPE_ADMIN')
  const isReadOnly = computed(() => (role.value as string) === 'READ_ONLY')

  /** 检查是否拥有指定权限 */
  function hasPermission(permission: Permission): boolean {
    return permissions.value.includes(permission)
  }

  // ---- actions ----
  async function login(user: { username: string; password: string }) {
    const result = await loginApi(user)
    
    // 适配后端返回格式：{ token, user: {...} }
    token.value = result.token
    username.value = result.user.username
    role.value = result.user.role
    tenantId.value = result.user.tenant_id || result.user.tenantId
    scopeIds.value = result.user.scopeIds || result.user.scope_ids || []
    // 根据角色获取权限列表
    permissions.value = getPermissionsByRole(result.user.role)

    localStorage.setItem('token', result.token)
    localStorage.setItem('username', result.user.username)
    localStorage.setItem('role', result.user.role)
    localStorage.setItem('tenantId', result.user.tenant_id || result.user.tenantId || '')
    localStorage.setItem('scopeIds', JSON.stringify(result.user.scopeIds || []))
    localStorage.setItem('permissions', JSON.stringify(permissions.value))
    return result
  }

  async function logout() {
    // JWT 无状态：后端 logout 仅记录审计，无论成功与否都要清本地 token
    try {
      await logoutApi()
    } catch {
      // 后端 logout 失败（token 已失效等）不影响前端登出
    }
    token.value = ''
    username.value = ''
    role.value = ''
    tenantId.value = ''
    scopeIds.value = []
    permissions.value = []

    localStorage.removeItem('token')
    localStorage.removeItem('username')
    localStorage.removeItem('role')
    localStorage.removeItem('tenantId')
    localStorage.removeItem('scopeIds')
    localStorage.removeItem('permissions')
  }

  return {
    token,
    username,
    role,
    tenantId,
    scopeIds,
    permissions,
    isLoggedIn,
    isSuperAdmin,
    isPlatformAdmin,
    isSecurityAdmin,
    isScopeAdmin,
    isReadOnly,
    hasPermission,
    login,
    logout,
  }
})
