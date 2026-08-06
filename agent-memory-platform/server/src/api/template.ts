/** 配置模板 API — 2026-07-19 P0-3 v3 重构
 *  端点前缀 /api/v1/config/templates
 *  类型：SCOPE（应用到租户）/ INSTANCE（实例单例）
 */
import { api } from './request'
import type {
  Template,
  TemplateType,
  TemplateApplyResult,
  TemplateDeleteResult,
} from '@/types/config'
import type { ApplyTemplateRequest, UpdateTemplateRequest } from '@/types/template'

function normalizeApplyResult(raw: any): TemplateApplyResult {
  return {
    templateId: raw.template_id ?? raw.templateId,
    templateName: raw.template_name ?? raw.templateName,
    templateType: raw.template_type ?? raw.templateType,
    results: Array.isArray(raw.results)
      ? raw.results.map((item: any) => ({
          tenantId: item.tenant_id ?? item.tenantId,
          tenantName: item.tenant_name ?? item.tenantName,
          success: item.success,
          errorMessage: item.error_message ?? item.errorMessage,
          currentVersion: item.current_version ?? item.currentVersion,
        }))
      : [],
    successCount: raw.success_count ?? raw.successCount ?? 0,
    failCount: raw.fail_count ?? raw.failCount ?? 0,
    restartTriggered: raw.restart_triggered ?? raw.restartTriggered,
    restartStatus: raw.restart_status ?? raw.restartStatus,
  }
}

function normalizeTemplate(raw: any): Template {
  return {
    id: raw.id,
    template_name: raw.template_name,
    display_name: raw.display_name,
    description: raw.description,
    template_type: raw.template_type,
    is_builtin: raw.is_builtin,
    parent_id: raw.parent_id,
    version: raw.version,
    config_json: raw.config_json,
    created_at: raw.created_at,
    updated_at: raw.updated_at,
    created_by: raw.created_by,
    tenant_usage: Array.isArray(raw.tenant_usage)
      ? raw.tenant_usage.map((item: any) => ({
          tenantId: item.tenant_id ?? item.tenantId,
          tenantName: item.tenant_name ?? item.tenantName,
        }))
      : undefined,
  }
}

/** 列出所有模板（按 type + isBuiltin 过滤）
 *  后端已内联 tenant_usage，前端直接按 row.tenant_usage 渲染
 */
export function listTemplates(type?: TemplateType, isBuiltin?: boolean): Promise<Template[]> {
  const params = new URLSearchParams()
  if (type) params.set('type', type)
  if (isBuiltin !== undefined) params.set('is_builtin', String(isBuiltin))
  const qs = params.toString()
  return api
    .get<any[]>(`/api/v1/config/templates${qs ? '?' + qs : ''}`)
    .then((list) => list.map(normalizeTemplate))
}

/** 查询单个模板 */
export function getTemplate(id: string): Promise<Template> {
  return api.get<Template>(`/api/v1/config/templates/${id}`)
}

/** 创建模板（INSTANCE 类型自动应用 + INSTANCE_CONFIG_UPDATE 审计） */
export function createTemplate(data: {
  template_name: string
  display_name?: string
  description?: string
  template_type: TemplateType
  config_json: string
  parent_id?: string
  target_tenant_ids?: string[]
  reason?: string
}): Promise<TemplateApplyResult> {
  return api.post<any>('/api/v1/config/templates', data).then(normalizeApplyResult)
}

/** 复制模板（parent_id 指向源模板） */
export function copyTemplate(
  sourceId: string,
  data: {
    template_name?: string
    display_name?: string
    description?: string
    template_type?: TemplateType
    config_json?: string
    target_tenant_ids?: string[]
    reason?: string
  }
): Promise<TemplateApplyResult> {
  return api.post<any>(`/api/v1/config/templates/${sourceId}/copy`, data).then(normalizeApplyResult)
}

/** 更新模板（预置不可改） */
export function updateTemplate(
  id: string,
  data: UpdateTemplateRequest
): Promise<Template> {
  return api.put<Template>(`/api/v1/config/templates/${id}`, {
    display_name: data.display_name,
    description: data.description,
    config_json: data.config_json,
    reason: data.reason,
    apply: data.apply,
    restart: data.restart,
    confirm_token: data.confirmToken,
    // SCOPE 模板编辑保存时一并提交目标租户，后端会与已绑定租户合并后应用
    target_tenant_ids: data.targetTenantIds,
  })
}

/** 删除模板（预置不可删；有绑定时级联清理内核 scope 配置 + DB 绑定记录） */
export function deleteTemplate(id: string): Promise<TemplateDeleteResult> {
  return api.delete<any>(`/api/v1/config/templates/${id}`).then((raw: any) => ({
    templateId: raw.template_id ?? raw.templateId ?? '',
    templateName: raw.template_name ?? raw.templateName,
    cleanedScopes: Array.isArray(raw.cleaned_scopes ?? raw.cleanedScopes)
      ? (raw.cleaned_scopes ?? raw.cleanedScopes).map((item: any) => ({
          tenantId: item.tenant_id ?? item.tenantId ?? '',
          tenantName: item.tenant_name ?? item.tenantName,
          scopeId: item.scope_id ?? item.scopeId,
          kernelDeleted: item.kernel_deleted ?? item.kernelDeleted ?? false,
          dbBindingDeleted: item.db_binding_deleted ?? item.dbBindingDeleted ?? false,
          errorMessage: item.error_message ?? item.errorMessage,
        }))
      : [],
    kernelSuccessCount: raw.kernel_success_count ?? raw.kernelSuccessCount ?? 0,
    kernelFailCount: raw.kernel_fail_count ?? raw.kernelFailCount ?? 0,
  }))
}

/** 应用模板到租户（SCOPE 必填 targetTenantIds，INSTANCE 支持 restart） */
export function applyTemplate(
  templateId: string,
  data: ApplyTemplateRequest
): Promise<TemplateApplyResult> {
  return api.post<any>(`/api/v1/config/templates/apply`, {
    template_id: templateId,
    target_tenant_ids: data.targetTenantIds,
    reason: data.reason,
    restart: data.restart,
    confirm_token: data.confirmToken,
  }).then(normalizeApplyResult)
}

