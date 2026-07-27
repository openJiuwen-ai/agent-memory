import request from './request'
import type {
  ScopeRegistry,
  CreateScopeRequest,
  UpdateScopeRequest,
} from '@/types/scope'

/**
 * 将后端返回的Scope数据转换为前端格式
 * 后端字段：scope_id, scope_name (snake_case)
 * 前端字段：scopeId, scopeName (camelCase)
 */
function mapScopeData(rawData: any[]): ScopeRegistry[] {
  return rawData.map(item => ({
    id: item.id,
    scopeId: item.scope_id || item.scopeId,
    scopeName: item.scope_name || item.scopeName,
    description: item.description,  // 新增：Scope描述
    status: item.status,
    assignedToTenantId: item.assigned_to_tenant_id || item.assignedToTenantId,
    createdAt: item.created_at || item.createdAt,
    updatedAt: item.updated_at || item.updatedAt,
  }))
}

/**
 * 获取所有Scope列表
 * 调用后端 GET /api/v1/scopes
 */
export async function getAllScopes(): Promise<ScopeRegistry[]> {
  try {
    const rawData: any[] = await request.get('/api/v1/scopes')
    return mapScopeData(rawData)
  } catch (error) {
    console.error('获取Scope列表失败:', error)
    throw error
  }
}

/**
 * 获取可分配的Scope列表（unassigned状态）
 * 调用后端 GET /api/v1/scopes/available
 */
export async function getAvailableScopes(): Promise<ScopeRegistry[]> {
  try {
    const rawData: any[] = await request.get('/api/v1/scopes/available')
    return mapScopeData(rawData)
  } catch (error) {
    console.error('获取可分配Scope列表失败:', error)
    throw error
  }
}

/**
 * 获取已分配的Scope列表
 * 调用后端 GET /api/v1/scopes/assigned
 */
export async function getAssignedScopes(): Promise<ScopeRegistry[]> {
  try {
    const rawData: any[] = await request.get('/api/v1/scopes/assigned')
    return mapScopeData(rawData)
  } catch (error) {
    console.error('获取已分配Scope列表失败:', error)
    throw error
  }
}

/**
 * 根据租户ID获取Scope列表
 * 调用后端 GET /api/v1/scopes/tenant/{tenantId}
 */
export async function getScopesByTenantId(tenantId: string): Promise<ScopeRegistry[]> {
  try {
    const rawData: any[] = await request.get(`/api/v1/scopes/tenant/${tenantId}`)
    return mapScopeData(rawData)
  } catch (error) {
    console.error('获取租户Scope列表失败:', error)
    throw error
  }
}

/**
 * 修改租户的Scope分配
 * 调用后端 PUT /api/v1/scopes/tenant/{tenantId}
 */
export async function updateTenantScopes(
  tenantId: string,
  data: { scopeIds: string[]; oldScopeIds: string[] }
): Promise<void> {
  try {
    await request.put(`/api/v1/scopes/tenant/${tenantId}`, data)
  } catch (error) {
    console.error('更新租户Scope分配失败:', error)
    throw error
  }
}

/**
 * 创建Scope
 */
export async function createScope(data: CreateScopeRequest): Promise<ScopeRegistry> {
  try {
    const rawData: any = await request.post('/api/v1/scopes', data)
    return mapScopeData([rawData])[0]
  } catch (error) {
    console.error('创建Scope失败:', error)
    throw error
  }
}

/**
 * 更新Scope
 */
export async function updateScope(scopeId: string, data: UpdateScopeRequest): Promise<ScopeRegistry> {
  try {
    const rawData: any = await request.put(`/api/v1/scopes/${scopeId}`, data)
    return mapScopeData([rawData])[0]
  } catch (error) {
    console.error('更新Scope失败:', error)
    throw error
  }
}

/**
 * 删除Scope
 */
export async function deleteScope(scopeId: string): Promise<void> {
  try {
    await request.delete(`/api/v1/scopes/${scopeId}`)
  } catch (error) {
    console.error('删除Scope失败:', error)
    throw error
  }
}
