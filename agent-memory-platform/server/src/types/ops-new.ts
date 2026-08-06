export interface OpsPageResult {
  health: { status: string; uptime: number }
  status: { memory_count: number; active_users: number }
  tasks: Array<{ id: string; name: string; status: string }>
  dreaming_status: { enabled: boolean; running: boolean; last_run: string }
  governance_summary: {
    active_cleanup_tasks: number
    last_scan_issues: number
    compliance_violations: number
    quota_usage_percent: number
  }
  trace_summary: { total_traced_memories: number; recent_corrections: number }
}
