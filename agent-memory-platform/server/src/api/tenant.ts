import request from './request'
import type {
  Tenant,
  TenantCreateForm,
} from '@/types/tenant'

/**
 * 获取租户列表
 * 调用后端 GET /api/v1/tenants
 */
export async function getTenantList(query?: { keyword?: string; page?: number; pageSize?: number }): Promise<{ list: Tenant[]; total: number }> {
  try {
    const tenants: any[] = await request.get('/api/v1/tenants')
    // 转换后端字段名为前端期望的格式
    const mappedTenants: Tenant[] = tenants.map(t => ({
      id: t.id,
      name: t.name,
      status: t.status,
      // 兼容后端返回的 scope_ids 和前端的 scopeIds
      scopeIds: t.scope_ids ? JSON.parse(t.scope_ids) : (t.scopeIds ? JSON.parse(t.scopeIds) : []),
      createTime: t.created_at,  // created_at -> createTime
      updateTime: t.updated_at,  // updated_at -> updateTime
      remark: t.remark,
      currentTemplateId: t.current_template_id ?? t.currentTemplateId,
      currentTemplateName: t.current_template_name ?? t.currentTemplateName,
    }))
    return {
      list: mappedTenants,
      total: mappedTenants.length
    }
  } catch (error) {
    // 错误已在 request.ts 中处理并显示提示
    console.error('获取租户列表失败:', error)
    throw error
  }
}

/**
 * 获取租户详情
 * 调用后端 GET /api/v1/tenants/{tenantId}
 */
export function getTenantDetail(tenantId: string): Promise<Tenant> {
  return request.get(`/api/v1/tenants/${tenantId}`)
}

/**
 * 创建租户
 * 调用后端 POST /api/v1/tenants
 */
export function createTenant(form: TenantCreateForm): Promise<Tenant> {
  return request.post('/api/v1/tenants', {
    name: form.name,
    remark: form.remark,
    scopeIds: form.scopeIds || [],
  })
}

/**
 * 更新租户
 * 调用后端 PUT /api/v1/tenants/{tenantId}
 */
export function updateTenant(tenantId: string, data: {
  name?: string
  remark?: string
  status?: string
  scopeIds?: string[]
}): Promise<Tenant> {
  return request.put(`/api/v1/tenants/${tenantId}`, data)
}

/**
 * 删除租户
 * 调用后端 DELETE /api/v1/tenants/{tenantId}
 */
export function deleteTenant(tenantId: string): Promise<void> {
  return request.delete(`/api/v1/tenants/${tenantId}`)
}

/* ===================== 租户成员（保留兼容，2026-07-17 deprecated） ===================== */

import { api } from './request'
import type { TenantMember } from '@/types/tenant'

/** @deprecated */
export function getTenantMembers(_tenantId: string): Promise<TenantMember[]> {
  return Promise.resolve([])
}

/** @deprecated */
export function createTenantMember(_tenantId: string, _data: Partial<TenantMember>): Promise<TenantMember> {
  return api.post<TenantMember>(`/api/v1/tenants/${_tenantId}/members`, _data)
}

/** @deprecated */
export function updateTenantMember(
  _tenantId: string,
  _memberId: string,
  _data: Partial<TenantMember>
): Promise<TenantMember> {
  return api.put<TenantMember>(`/api/v1/tenants/${_tenantId}/members/${_memberId}`, _data)
}

/** @deprecated */
export function deleteTenantMember(_tenantId: string, _memberId: string): Promise<void> {
  return api.delete<void>(`/api/v1/tenants/${_tenantId}/members/${_memberId}`)
}
