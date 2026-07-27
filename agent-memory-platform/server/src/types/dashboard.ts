export interface DashboardResult {
  system_status: string
  tenant_count: number
  total_scopes: number
  total_users: number
  total_memories: number
  active_tasks: number
  recent_errors: number
  store_types: Record<string, string>
  memory_trend: Array<{ date: string; count: number }>
  error_trend: Array<{ date: string; count: number }>
}
