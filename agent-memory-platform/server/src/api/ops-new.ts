import type { OpsPageResult } from '@/types/ops-new'
import request from '@/api/request'

// 集成架构：前端 → 平台(platform, /api/v1/ops/*) → 记忆系统(:8516)。
// health-probe / memory-count 走平台 /api/v1/ops/tools/*。

function delay<T>(data: T, ms = 400): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(data), ms))
}

export function getOpsPage(): Promise<OpsPageResult> {
  return delay({
    health: { status: 'healthy', uptime: 3600 },
    status: { memory_count: 1523, active_users: 30 },
    tasks: [{ id: 'task-dreaming-1', name: 'Dreaming 巩固', status: 'running' }],
    dreaming_status: { enabled: true, running: true, last_run: '2026-07-08T10:00:00Z' },
    governance_summary: {
      active_cleanup_tasks: 1,
      last_scan_issues: 3,
      compliance_violations: 0,
      quota_usage_percent: 1.52,
    },
    trace_summary: { total_traced_memories: 1523, recent_corrections: 2 },
  })
}

/** 健康探测：GET /api/v1/ops/tools/health-probe → {status, message} */
export function getHealthProbe(): Promise<{ status: string; message: string }> {
  return request.get('/api/v1/ops/tools/health-probe').then((r: any) => r)
}

/** 记忆计数：GET /api/v1/ops/tools/memory-count → {count, approximate, hint} */
export function getMemoryCount(
  userId?: string,
  scopeId?: string,
  memoryType?: string,
): Promise<{ count: number; approximate: boolean; hint?: string }> {
  return request
    .get('/api/v1/ops/tools/memory-count', {
      params: {
        user_id: userId || undefined,
        scope_id: scopeId || undefined,
        memory_type: memoryType || undefined,
      },
    })
    .then((r: any) => r)
}
