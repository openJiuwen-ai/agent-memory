/** 任务管理类型定义（F8） */

export type TaskType = 'DREAMING' | 'MIGRATION'
export type TaskStatus = 'pending' | 'running' | 'stopped' | 'failed' | 'completed'

/** task_registry 记录 */
export interface TaskRecord {
  id: string
  admin_user_id: string
  scope_name: string
  user_id?: string
  task_type: TaskType
  task_config: Record<string, any>
  status: TaskStatus
  started_at?: string
  stopped_at?: string
  last_heartbeat?: string
  error_message?: string
  created_by: string
  created_at: string
}

/** Dreaming 运行状态 */
export interface DreamingStatus {
  scope_id: string
  user_id: string
  running: boolean
  interval_seconds: number
}

/** Dreaming 运行时间与产出结果 */
export interface DreamingResult {
  scope_id: string
  user_id: string
  running: boolean
  interval_seconds: number
  last_scan_ts?: string
  next_estimated_ts?: string
  scanned_sessions_count: number
  last_promoted_count?: number
  error?: string
}

/** 列表查询参数 */
export interface TaskListQuery {
  task_type?: TaskType
  status?: TaskStatus
}

/** 列表结果 */
export interface TaskListResult {
  total: number
  items: TaskRecord[]
}
