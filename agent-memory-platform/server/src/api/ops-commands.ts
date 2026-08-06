import request from '@/api/request'
import type {
  OpsCommand,
  CommandExecution,
  DispatchRequest,
  DispatchResult,
  ExecutionsQuery,
  PageResult,
} from '@/types/ops-commands'

// 集成架构：前端 → 平台(platform, /api/v1/ops/commands) → 记忆系统(:8516)。
// dispatch body 用 snake_case（平台 SNAKE_CASE 反序列化）。

/** 命令目录（?category 过滤）。 */
export function listCommands(category?: string): Promise<OpsCommand[]> {
  return request
    .get('/api/v1/ops/commands', { params: { category: category || undefined } })
    .then((r: any) => (r as OpsCommand[]) || [])
}

/** 下发命令。dry_run=true 预演；HEALTH 真调 :8516 /health；缺口命令返 status=gap(50010)。 */
export function dispatchCommand(req: DispatchRequest): Promise<DispatchResult> {
  return request
    .post('/api/v1/ops/commands/dispatch', {
      command_code: req.commandCode,
      scope_id: req.scopeId || undefined,
      user_id: req.userId || undefined,
      payload: req.payload || undefined,
      dry_run: req.dryRun ?? false,
      reason: req.reason || undefined,
    })
    .then((r: any) => r as DispatchResult)
}

/** 执行历史（按 command_code/status 过滤，分页）。 */
export function listExecutions(query?: ExecutionsQuery): Promise<PageResult<CommandExecution>> {
  return request
    .get('/api/v1/ops/commands/executions', {
      params: {
        page_idx: query?.page_idx ?? 1,
        page_size: query?.page_size ?? 20,
        command_code: query?.command_code || undefined,
        status: query?.status || undefined,
      },
    })
    .then((r: any) => r as PageResult<CommandExecution>)
}

/** 单条执行详情。 */
export function getExecution(executionId: string): Promise<CommandExecution> {
  return request
    .get(`/api/v1/ops/commands/executions/${encodeURIComponent(executionId)}`)
    .then((r: any) => r as CommandExecution)
}
