import request from './request'
import type { UserRole } from '@/types/tenant'

/** 登录请求参数 */
export interface LoginParams {
  username: string
  password: string
}

/** 登录响应 */
export interface LoginResult {
  token: string
  user: {
    id: string
    tenant_id?: string           // 兼容旧版下划线命名
    tenantId?: string            // 新版驼峰命名
    username: string
    role: UserRole
    scope_ids?: string[] | null   // 兼容旧版下划线命名
    scopeIds?: string[] | null     // 新版驼峰命名
    remark: string
  }
}

/** 当前用户信息（由 /auth/info 返回，不含 password） */
export interface UserInfo {
  id: string
  tenant_id?: string           // 兼容旧版下划线命名
  tenantId?: string            // 新版驼峰命名
  username: string
  role: UserRole
  scope_ids?: string[] | null   // 兼容旧版下划线命名
  scopeIds?: string[] | null     // 新版驼峰命名
  remark: string
}

/**
 * 登录接口
 * 调用后端 POST /api/v1/auth/login
 */
export function login(data: LoginParams): Promise<LoginResult> {
  return request.post('/api/v1/auth/login', data)
}

/**
 * 退出登录
 * 调用后端 POST /api/v1/auth/logout（JWT 无状态，后端仅记录审计；前端清 token）
 */
export function logout(): Promise<void> {
  return request.post('/api/v1/auth/logout')
}

/**
 * 获取当前用户信息（从 JWT 解析 userId 查 DB，不含 password）
 * 调用后端 GET /api/v1/auth/info
 */
export function getUserInfo(): Promise<UserInfo> {
  return request.get('/api/v1/auth/info')
}

/**
 * 获取角色列表
 * 调用后端 GET /api/v1/roles
 */
export function getRoleList(): Promise<string[]> {
  return request.get('/api/v1/roles')
}
