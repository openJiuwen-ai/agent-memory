// 集成架构：前端 → 记忆服务平台(platform, /api/v1/ops/trace/*) → 记忆系统(:8516)。
// 本调用面向【平台】端点；平台读本地 memory_change_log_snapshot 快照 + :8516 当前态。
// 来源消息（:8516 get_message_by_id 缺 HTTP）与血缘（dreaming 不写）暂空，UI 降级提示。
import request from '@/api/request'

/** 追溯全链路 bundle。前端直接传入记忆字段，避免后端翻页查找。 */
export function getTracePage(mem_id: string, user_id?: string, scope_id?: string, memItem?: {
  content?: string
  type?: string
  timestamp?: string
  source_id?: string
}): Promise<any> {
  return request
    .get(`/api/v1/ops/trace/memory/${encodeURIComponent(mem_id)}`, {
      params: {
        user_id: user_id || undefined,
        scope_id: scope_id || undefined,
        content: memItem?.content || undefined,
        mem_type: memItem?.type || undefined,
        timestamp: memItem?.timestamp || undefined,
        source_id: memItem?.source_id || undefined,
      },
    })
    .then((res: any) => res)
}

/** 变更历史。 */
export function getTraceHistory(mem_id: string): Promise<any> {
  return request.get(`/api/v1/ops/trace/memory/${encodeURIComponent(mem_id)}/history`).then((r: any) => r)
}

/** 操作审计。 */
export function getTraceAudit(mem_id: string): Promise<any> {
  return request.get(`/api/v1/ops/trace/memory/${encodeURIComponent(mem_id)}/audit`).then((r: any) => r)
}
