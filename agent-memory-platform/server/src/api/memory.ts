import type {
  MemoryBrowseQuery,
  MemoryBrowseResult,
  MemoryRecord,
  MemorySearchQuery,
  MemorySearchResult,
  ListMemoriesResult,
  PlatformMemoryItem,
  MemoryType,
} from '@/types/memory'
import request from '@/api/request'

// 集成架构：前端 → 平台(platform, /api/v1/ops/*) → 记忆系统(:8516)。
// 下列调用面向【平台】端点（平台全局 SNAKE_CASE：mem_id/scope_id/page_idx 等）。

/* ---------------- 记忆浏览（真实：对接平台 GET /api/v1/ops/memory） ---------------- */

/**
 * 浏览记忆（聚合：列表 + 记忆类型集合）。平台 MemoryItem 含 mem_id/content/type/user_id/scope_id/timestamp。
 * 注意：本页 page_idx 为 0 基，平台 page_idx 为 1 基，这里 +1 转换。
 */
export function browseMemories(query: MemoryBrowseQuery): Promise<MemoryBrowseResult> {
  return listMemories({
    scope_id: query.scope_id || undefined,
    user_id: query.user_id || undefined,
    memory_type: query.memory_type || undefined,
    page_idx: (query.page_idx ?? 0) + 1,
    page_size: query.page_size,
  }).then((res) => {
    const items = res.items || []
    const memories: MemoryRecord[] = items.map((m) => ({
      mem_id: m.mem_id,
      content: m.content,
      memory_type: (m.type as MemoryType) ?? 'summary',
      scope_id: m.scope_id || query.scope_id || '__default__',
      user_id: m.user_id || query.user_id || '__default__',
      created_at: m.timestamp || '',
      updated_at: m.timestamp || '',
    }))
    const memory_types = [...new Set(items.map((m) => m.type))] as MemoryType[]
    return {
      memories,
      total: res.total ?? memories.length,
      memory_types,
      scope_info: {
        scope_id: query.scope_id || '__default__',
        scope_name: '',
        has_config: false,
      },
      variables: {},
    }
  })
}

/* ---------------- F5 记忆列表（真实：对接平台 GET /api/v1/ops/memory） ---------------- */

/** 列表查询（对接平台）。平台 MemoryItem 仅含 mem_id/content/type（全局 SNAKE_CASE）；scopeId/userId 用查询条件回填。 */
export function listMemories(query: MemoryBrowseQuery): Promise<ListMemoriesResult> {
  return request
    .get('/api/v1/ops/memory', {
      params: {
        user_id: query.user_id || undefined,
        scope_id: query.scope_id || undefined,
        memory_type: query.memory_type || undefined,
        page_idx: query.page_idx,
        page_size: query.page_size,
      },
    })
    .then((res: any) => res as ListMemoriesResult)
}

/* ---------------- 记忆搜索（真实：对接平台 POST /api/v1/ops/tools/search-memory） ---------------- */

/** 搜索记忆。平台返回项含 score；按 {mem_id,content,type,score} → {mem_id,content,memory_type,score} 映射。 */
export function searchMemories(query: MemorySearchQuery): Promise<MemorySearchResult[]> {
  return request
    .post('/api/v1/ops/tools/search-memory', {
      query: query.query,
      num: query.num,
      user_id: query.user_id || undefined,
      scope_id: query.scope_id || undefined,
      threshold: query.threshold,
    })
    .then((res: any) => {
      const items = (res as PlatformMemoryItem[]) || []
      return items.map((m) => ({
        mem_id: m.mem_id,
        content: m.content,
        memory_type: m.type as MemoryType,
        score: m.score ?? 0,
      }))
    })
}

/* ---------------- 记忆删除（真实：对接平台 DELETE /api/v1/ops/memory/{memId}） ---------------- */

/**
 * 删除单条记忆。需 user_id/scope_id 定位（平台按页定位）。
 * ⚠️ 已知缺口：:8516 未暴露 delete_mem_by_id，平台抛 GapException(50010)，删除会失败——此处如实对接，错误由拦截器提示。
 */
/** 删除单条记忆。需 user_id/scope_id 定位。old_content 为变更前内容，用于快照留痕。 */
export function deleteMemory(mem_id: string, user_id?: string, scope_id?: string, old_content?: string): Promise<void> {
  return request
    .delete(`/api/v1/ops/memory/${encodeURIComponent(mem_id)}`, {
      params: {
        user_id: user_id || undefined,
        scope_id: scope_id || undefined,
        reason: 'frontend delete',
        old_content: old_content || undefined,
      },
    })
    .then(() => undefined)
}

/* ---------------- 批量删除（真实：对接平台 POST /api/v1/ops/memory/batch-delete） ---------------- */

/** 批量删除。⚠️ 命中 :8516 batch_delete_mem 缺口（50010），:8516 补端点后生效。 */
export function batchDeleteMemories(mem_ids: string[], user_id?: string, scope_id?: string): Promise<any> {
  return request
    .post('/api/v1/ops/memory/batch-delete', {
      mem_ids,
      user_id: user_id || undefined,
      scope_id: scope_id || undefined,
      reason: 'frontend batch delete',
    })
    .then((res: any) => res)
}

/* ---------------- 记忆更新（真实：对接平台 PUT /api/v1/ops/memory/{memId}） ---------------- */

/** 更新记忆内容。需 user_id/scope_id 定位。old_content 为变更前内容，用于快照留痕。 */
export function updateMemory(mem_id: string, content: string, user_id?: string, scope_id?: string, reason?: string, old_content?: string): Promise<void> {
  return request
    .put(`/api/v1/ops/memory/${encodeURIComponent(mem_id)}`, {
      memory: content,
      user_id: user_id || undefined,
      scope_id: scope_id || undefined,
      reason: reason || 'frontend edit',
      old_content: old_content || undefined,
    })
    .then(() => undefined)
}

/* ---------------- 获取用户变量（真实：对接平台 GET /api/v1/ops/memory/variables） ---------------- */

export function getUserVariables(user_id?: string, scope_id?: string, names?: string[]): Promise<Record<string, string>> {
  return request
    .get('/api/v1/ops/memory/variables', {
      params: {
        user_id: user_id || undefined,
        scope_id: scope_id || undefined,
        names: names,
      },
    })
    .then((res: any) => (res as Record<string, string>) || {})
}

/* ---------------- 更新/新增用户变量（真实：对接平台 PUT /api/v1/ops/memory/variables） ---------------- */

/**
 * 更新/新增用户变量。传 {变量名: 值} map，:8516 按键合并写入。
 * 新增变量 = 传一个新键；修改变量 = 传已存在键的新值。
 */
export function updateUserVariables(user_id?: string, scope_id?: string, variables: Record<string, string> = {}): Promise<any> {
  return request
    .put('/api/v1/ops/memory/variables', {
      user_id: user_id || undefined,
      scope_id: scope_id || undefined,
      variables,
    })
    .then((res: any) => res)
}

/* ---------------- 删除用户变量（真实：对接平台 DELETE /api/v1/ops/memory/variables） ---------------- */

/** 删除用户变量。names 为要删的变量名列表。 */
export function deleteUserVariables(user_id?: string, scope_id?: string, names: string[] = []): Promise<any> {
  return request
    .delete('/api/v1/ops/memory/variables', {
      data: {
        user_id: user_id || undefined,
        scope_id: scope_id || undefined,
        names,
      },
    })
    .then((res: any) => res)
}
