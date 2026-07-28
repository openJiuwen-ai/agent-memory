/**
 * 配置中心类型定义 — 2026-07-19 P0-3 v3 重构
 *
 * 架构变化：
 * - 1 tenant = 1 scope (UUID 同体)
 * - 2 种模板类型：SCOPE（应用到租户）/ INSTANCE（单例，对应 instance_config）
 * - 模板列表 2 个 Tab：系统默认（is_builtin=true）+ 自定义（is_builtin=false）
 * - 删除：ScopeRegistry / ScopeConfig / 4层继承链 / Dreaming sys_default
 * - Dreaming 引擎级配置已合并到 INSTANCE 系统模板，不再单独管理
 */

// =============== 模板 ===============

/** 模板类型：SCOPE 应用到租户，INSTANCE 实例级单例 */
export type TemplateType = 'SCOPE' | 'INSTANCE'

/** 模板使用租户（列表页渲染用） */
export interface TemplateTenantUsage {
  tenantId: string
  tenantName: string
}

/** 模板状态：published=已发布，draft=草稿 */
export type TemplateStatus = 'published' | 'draft'

/** 模板 */
export interface Template {
  id: string
  template_name: string
  display_name?: string
  description?: string
  template_type: TemplateType
  is_builtin?: boolean
  parent_id?: string | null
  version?: number
  config_json: string
  /** 状态：published=已发布，draft=草稿 */
  status?: TemplateStatus
  created_at?: string
  updated_at?: string
  created_by?: string
  tenant_usage?: TemplateTenantUsage[]
}

/** 模板应用结果 */
export interface TemplateApplyResult {
  templateId: string
  templateName?: string
  templateType: string
  results: Array<{
    tenantId: string
    tenantName?: string
    success: boolean
    errorMessage?: string
    currentVersion?: number
  }>
  successCount: number
  failCount: number
  /** INSTANCE 模板应用/更新时是否触发了引擎重启 */
  restartTriggered?: boolean
  /** 引擎重启状态描述 */
  restartStatus?: string
}

// =============== 租户级配置（1 tenant = 1 scope） ===============

/** 租户级 Scope 配置快照 DTO */
export interface TenantScopeConfig {
  tenantId: string
  tenantName: string
  /** scope 绑定列表（权威来源 scope_registry），为 1 租户:N scope 扩展留口 */
  scopeIds?: string[]
  /** 兼容旧字段：单条接口仍可能返回单个 scopeId */
  scopeId?: string
  instanceId: string
  /** 列表接口不返回（SQL 层已剔除大字段），仅单条/编辑场景有值 */
  configJson?: string
  templateId?: string | null
  templateName?: string
  templateVersion?: number | null
  currentVersion?: number | null
  isDeviated?: boolean
  updatedAt?: string
  updatedBy?: string
}


// =============== 实例级配置（单例） ===============

/** 实例级配置 DTO（id=1 单例） */
export interface InstanceConfig {
  templateId?: string | null
  configJson: string
  version: number
  updatedAt?: string
  updatedBy?: string
}

// =============== 内核配置（Push 模型） ===============

/** 内核参数 */
export interface KernelParam {
  value?: any
  configured?: boolean
  editable: boolean
  category?: 'architecture' | 'connection' | 'startup' | 'engine'
  danger?: 'critical' | 'warning' | 'safe'
}

/** 内核配置 */
export interface KernelConfig {
  runtime: Record<string, KernelParam>
  storage: Record<string, KernelParam>
  vector_engine: Record<string, KernelParam>
  engine: Record<string, KernelParam>
  restart_required: boolean
  source: string
  available?: boolean
  error?: string
}

export interface UpdateKernelConfigRequest {
  updates: Record<string, any>
  restart: boolean
  reason: string
  confirm_token?: string
}

export interface UpdateResult {
  status: 'success' | 'failed'
  updated_keys?: string[]
  rejected_keys?: string[]
  restart_triggered?: boolean
  restart_status?: string
  message?: string
  version?: number
  error?: string
}

// =============== 二次确认（P0-2） ===============

export interface ConfirmTokenIssueRequest {
  action: string
  resource?: string
  payload?: string
  ttl_minutes: number
}

export interface ConfirmTokenIssueResponse {
  /** 后端 ConfirmTokenController 返回的 Map key 为 "confirmToken"（camelCase，不受 SNAKE_CASE 影响） */
  confirmToken: string
  ttl_minutes: number
}

export interface ConfirmTokenValidateResponse {
  ok: boolean
  reason?: string
  payload?: string
  resource?: string
}
