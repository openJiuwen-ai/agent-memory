/** 操作审计日志 */
export interface AuditLog {
  id: string
  time: string
  operator: string
  operationType: string
  target: string
  result: 'success' | 'failed'
  duration: number
  detail?: string
}

/** 操作审计日志查询参数 */
export interface AuditLogQuery {
  admin_user_id?: string
  operator?: string
  type?: string
  start?: string
  end?: string
  page?: number
  size?: number
}

/** 操作审计日志查询结果 */
export interface AuditLogResult {
  results: AuditLog[]
  total: number
  page: number
}

/** 运行日志 */
export interface RuntimeLog {
  id: string
  time: string
  level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
  eventType: string
  module?: string
  message: string
  detail?: string
}

/** 运行日志查询参数 */
export interface RuntimeLogQuery {
  level?: string
  eventType?: string
  module?: string
  start?: string
  end?: string
  page?: number
  size?: number
}

/** 运行日志查询结果 */
export interface RuntimeLogResult {
  results: RuntimeLog[]
  total: number
  page: number
}

/** 用户消息日志 */
export interface MessageLog {
  id: string
  time: string
  requestId: string
  scopeId: string
  userId?: string
  apiPath: string
  status: number
  duration: number
  requestParams?: string
  response?: string
}

/** 用户消息日志查询参数 */
export interface MessageLogQuery {
  start?: string
  end?: string
  scopeId?: string
  status?: number
  page?: number
  size?: number
}

/** 用户消息日志查询结果 */
export interface MessageLogResult {
  results: MessageLog[]
  total: number
  page: number
}

/** 日志统计 */
export interface LogStatistics {
  totalOperations: number
  successRate: number
  avgDuration: number
  errorCount: number
}

/** 日志级别 */
export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
