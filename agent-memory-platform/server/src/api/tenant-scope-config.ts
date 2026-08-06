/** 租户级 Scope 配置 API — 2026-07-17 P0-3 v2
 *  1 tenant = 1 scope（UUID 同体），每行 = 租户的"应用配置快照"
 *  端点前缀 /api/v1/tenant-scope-configs
 */
import { api } from './request'
import type { TenantScopeConfig, TenantScopeConfigDeleteResult } from '@/types/config'

function normalizeTenantScopeConfig(raw: any): TenantScopeConfig {
  // 列表接口返回 scope_ids 数组；单条接口返回 scope_id 单值 —— 统一规整成 scopeIds 数组
  const scopeIds: string[] = Array.isArray(raw.scope_ids)
    ? raw.scope_ids
    : Array.isArray(raw.scopeIds)
      ? raw.scopeIds
      : (raw.scope_id ?? raw.scopeId)
        ? [raw.scope_id ?? raw.scopeId]
        : []
  return {
    tenantId: raw.tenant_id ?? raw.tenantId,
    tenantName: raw.tenant_name ?? raw.tenantName,
    scopeIds,
    scopeId: raw.scope_id ?? raw.scopeId,
    instanceId: raw.instance_id ?? raw.instanceId,
    configJson: raw.config_json ?? raw.configJson,
    templateId: raw.template_id ?? raw.templateId,
    templateName: raw.template_name ?? raw.templateName,
    templateVersion: raw.template_version ?? raw.templateVersion,
    currentVersion: raw.current_version ?? raw.currentVersion,
    isDeviated: raw.is_deviated ?? raw.isDeviated,
    updatedAt: raw.updated_at ?? raw.updatedAt,
    updatedBy: raw.updated_by ?? raw.updatedBy,
  }
}

/** 查租户的 Scope 配置快照 */
export function getTenantScopeConfig(tenantId: string): Promise<TenantScopeConfig> {
  return api.get<TenantScopeConfig>(`/api/v1/tenant-scope-configs/${tenantId}`).then((res: any) => {
    if (!res) return res
    return normalizeTenantScopeConfig(res)
  })
}

/** 租户修改自己的参数（不影响其他租户） */
export function updateTenantScopeConfig(
  tenantId: string,
  configJson: string
): Promise<TenantScopeConfig> {
  return api
    .put<TenantScopeConfig>(`/api/v1/tenant-scope-configs/${tenantId}`, { configJson })
    .then((res: any) => (res ? normalizeTenantScopeConfig(res) : res))
}

/** 列出指定模板下的租户快照（后端 SQL 过滤，不含 config_json 大字段）。templateId 必填。 */
export function listTenantScopeConfigs(templateId: string): Promise<TenantScopeConfig[]> {
  return api
    .get<TenantScopeConfig[]>(`/api/v1/tenant-scope-configs?templateId=${encodeURIComponent(templateId)}`)
    .then((list: any) => (Array.isArray(list) ? list.map(normalizeTenantScopeConfig) : list))
}

/** 列出偏离模板的租户（templateVersion != currentVersion） */
export function listDeviatedTenantScopeConfigs(): Promise<TenantScopeConfig[]> {
  return api
    .get<TenantScopeConfig[]>(`/api/v1/tenant-scope-configs/deviated`)
    .then((list: any) => (Array.isArray(list) ? list.map(normalizeTenantScopeConfig) : list))
}

/** 平台操作：把租户快照同步回模板（即"以原模板下发"） */
export function syncTenantFromTemplate(tenantId: string): Promise<TenantScopeConfig> {
  return api
    .post<TenantScopeConfig>(`/api/v1/tenant-scope-configs/${tenantId}/sync-from-template`)
    .then((res: any) => (res ? normalizeTenantScopeConfig(res) : res))
}

/**
 * 清除租户的 Scope 配置：删除内核 KV 中的 scope 配置 + DB 绑定记录，
 * 使该租户回退到默认配置。租户本身不删除。
 * 适用场景：在租户管理页移除某租户的自定义配置。
 */
export function deleteTenantScopeConfig(tenantId: string): Promise<TenantScopeConfigDeleteResult> {
  return api
    .delete<any>(`/api/v1/tenant-scope-configs/${tenantId}`)
    .then((raw: any) => ({
      tenantId: raw.tenant_id ?? raw.tenantId,
      tenantName: raw.tenant_name ?? raw.tenantName,
      scopeId: raw.scope_id ?? raw.scopeId,
      kernelDeleted: raw.kernel_deleted ?? raw.kernelDeleted ?? false,
      dbBindingDeleted: raw.db_binding_deleted ?? raw.dbBindingDeleted ?? false,
      errorMessage: raw.error_message ?? raw.errorMessage,
    }))
}
