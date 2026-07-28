/** F1 远程运维命令类型（对齐平台 OpsCommandCatalogEntity / CommandExecutionLogEntity，全局 SNAKE_CASE） */

/** 命令目录项 */
export interface OpsCommand {
  command_code: string
  command_name: string
  category: string // inspection/admin/maintenance/task
  backend_action: string
  enabled: boolean
  gap_reason?: string | null
  require_confirm: boolean
  description?: string | null
}

/** 执行日志 */
export interface CommandExecution {
  execution_id: string
  command_code: string
  tenant_id?: string | null
  scope_id?: string | null
  user_id?: string | null
  payload_snapshot?: string | null
  result_snapshot?: string | null
  status: string // success/failed/gap/dry_run
  gap_hint?: string | null
  duration_ms?: number | null
  operator_id?: string | null
  request_ip?: string | null
  reason?: string | null
  created_at?: string | null
}

/** dispatch 请求 */
export interface DispatchRequest {
  commandCode: string
  scopeId?: string
  userId?: string
  payload?: Record<string, any>
  dryRun?: boolean
  reason?: string
}

/** dispatch 响应 */
export interface DispatchResult {
  execution_id: string
  command_code: string
  status: string
  result: any
  gap_hint?: string
  duration_ms: number
}

/** 执行历史查询 */
export interface ExecutionsQuery {
  page_idx?: number
  page_size?: number
  command_code?: string
  status?: string
}

/** 分页结果 */
export interface PageResult<T> {
  items: T[]
  total: number
  page_idx: number
  page_size: number
}
