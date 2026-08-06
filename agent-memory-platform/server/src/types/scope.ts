/** Scope注册表相关类型定义 */

/** Scope状态 */
export type ScopeStatus = 'unassigned' | 'assigned'

/** Scope注册表 */
export interface ScopeRegistry {
  id: string
  scopeId: string
  scopeName: string
  description?: string  // Scope描述
  status: ScopeStatus
  assignedToTenantId: string | null
  createdAt: string
  updatedAt: string
}

/** 创建Scope请求 */
export interface CreateScopeRequest {
  scopeId?: string        // 可选，不填则随机生成
  scopeName: string
  description?: string
}

/** 更新Scope请求 */
export interface UpdateScopeRequest {
  scopeName?: string
  description?: string
}

/** Scope列表查询结果 */
export interface ScopeListResult {
  list: ScopeRegistry[]
  total: number
}
