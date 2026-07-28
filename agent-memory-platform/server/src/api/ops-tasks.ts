import request from '@/api/request'
import type {
  TaskListQuery,
  TaskListResult,
  TaskRecord,
  DreamingStatus,
  DreamingResult,
} from '@/types/ops-tasks'

// 集成架构：前端 → 记忆服务平台(platform, /api/v1/ops/*) → 记忆系统(:8516)。
// 本文件所有调用面向【平台】端点；平台再对接 :8516。已全部对接真实端点。

/** 查询任务列表。对接平台 GET /api/v1/ops/tasks（platform 按租户返所有，前端按 type/status 过滤）。 */
export function listTasks(query?: TaskListQuery): Promise<TaskListResult> {
  return request
    .get('/api/v1/ops/tasks')
    .then((res: any) => {
      let items = (Array.isArray(res) ? res : res?.items || []) as TaskRecord[]
      if (query?.task_type) items = items.filter((t) => t.task_type === query.task_type)
      if (query?.status) items = items.filter((t) => t.status === query.status)
      return { total: items.length, items }
    })
}

/** 启动 Dreaming（幂等：重复 start 返已有）。对接平台 POST /api/v1/ops/tasks/dreaming/start。 */
export function startDreaming(scope_id: string, user_id: string): Promise<{ started: boolean; task_id: string }> {
  return request
    .post('/api/v1/ops/tasks/dreaming/start', {}, {
      params: { scope_id, user_id },
    })
    .then((r: any) => r)
}

/** 停止 Dreaming。对接平台 POST /api/v1/ops/tasks/dreaming/stop。 */
export function stopDreaming(scope_id: string, user_id: string): Promise<{ stopped: boolean }> {
  return request
    .post('/api/v1/ops/tasks/dreaming/stop', {}, {
      params: { scope_id, user_id },
    })
    .then((r: any) => r)
}

/** Dreaming 状态（active 列表）。对接平台 GET /api/v1/ops/tasks/dreaming/status。 */
export function getDreamingStatus(): Promise<DreamingStatus[]> {
  return request
    .get('/api/v1/ops/tasks/dreaming/status')
    .then((res: any) => {
      const list = (res?.orchestrators || []) as any[]
      return list.map((o) => ({
        scope_id: o.scope_id,
        user_id: o.user_id,
        running: !!o.running,
        interval_seconds: o.interval_seconds ?? 0,
      })) as DreamingStatus[]
    })
}

/** Dreaming 运行时间与产出结果。调 status 端点，按 scope/user 过滤出单条。 */
export function getDreamingResult(scope_id: string, user_id: string): Promise<DreamingResult> {
  return request
    .get('/api/v1/ops/tasks/dreaming/status')
    .then((res: any) => {
      const list = (res?.orchestrators || []) as any[]
      const hit = list.find((o) => o.scope_id === scope_id && o.user_id === user_id)
      return hit
        ? {
            scope_id: hit.scope_id,
            user_id: hit.user_id,
            running: !!hit.running,
            interval_seconds: hit.interval_seconds ?? 0,
            last_scan_ts: hit.last_scan_ts ?? undefined,
            next_estimated_ts: hit.next_estimated_ts ?? undefined,
            scanned_sessions_count: hit.scanned_sessions_count ?? 0,
            last_promoted_count: hit.last_promoted_count ?? undefined,
          } as DreamingResult
        : {
            scope_id,
            user_id,
            running: false,
            interval_seconds: 0,
            scanned_sessions_count: 0,
            last_promoted_count: undefined,
          } as DreamingResult
    })
}
