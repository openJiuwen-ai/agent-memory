/**
 * 模板类型定义 — 2026-07-19 P0-3 v3 重构
 *
 * 旧版键值对形式（TemplateParam[]）已废弃，改为单 config_json 字符串存储。
 * 前端 UI 可在 config_json textarea 中编辑完整 JSON。
 */

/** 模板应用请求 */
export interface ApplyTemplateRequest {
  /** SCOPE 模板必填：目标租户 ID 列表 */
  targetTenantIds?: string[]
  /** 操作原因（审计） */
  reason?: string
  /** INSTANCE 模板专用：是否触发引擎重启 */
  restart?: boolean
  /** INSTANCE 模板专用：restart=true 时必填的二次确认令牌 */
  confirmToken?: string
}

/** 模板更新请求 */
export interface UpdateTemplateRequest {
  display_name?: string
  description?: string
  config_json?: string
  reason?: string
  /** 是否应用到内核：true=保存并下发，false=仅保存草稿 */
  apply?: boolean
  /** INSTANCE 模板专用：是否触发引擎重启 */
  restart?: boolean
  /** INSTANCE 模板专用：restart=true 时必填的二次确认令牌 */
  confirmToken?: string
  /**
   * SCOPE 模板专用：编辑保存时指定要应用（绑定）的目标租户列表。
   * 为空时仅对已绑定租户重新下发；非空时与已绑定租户合并后一并应用。
   */
  targetTenantIds?: string[]
}

/** 模板对比结果（保留兼容） */
export interface TemplateCompareResult {
  template_config: any
  current_config: any
  diff: {
    added: string[]
    removed: string[]
    changed: Array<{
      key: string
      template_value: any
      current_value: any
    }>
  }
  scope_id: string
  scope_name: string
}

/** 模板应用记录（审计展示用） */
export interface TemplateApplyRecord {
  id: string
  templateId: string
  templateName: string
  /** 生效的 tenant_id */
  tenantId: string
  /** 操作类型 */
  action: 'apply' | 'hot_reload' | 'rollback'
  /** 操作人 */
  operator: string
  /** 操作结果 */
  result: 'success' | 'failed'
  /** 结果说明 */
  remark?: string
  time: string
}
