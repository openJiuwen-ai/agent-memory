/** 记忆模块相关类型定义（基于 730 详细设计 §5.3） */

/** 记忆类型 */
export type MemoryType =
  | 'user_profile'
  | 'semantic_memory'
  | 'episodic_memory'
  | 'summary'
  | 'variable'

/** 记忆记录 */
export interface MemoryRecord {
  mem_id: string
  content: string
  memory_type: MemoryType
  scope_id: string
  user_id: string
  created_at: string
  updated_at: string
}

/** 记忆浏览查询参数 */
export interface MemoryBrowseQuery {
  scope_id?: string
  user_id?: string
  memory_type?: MemoryType | ''
  page_idx: number
  page_size: number
}

/** 记忆浏览响应 */
export interface MemoryBrowseResult {
  memories: MemoryRecord[]
  total: number
  memory_types: MemoryType[]
  scope_info: {
    scope_id: string
    scope_name: string
    has_config: boolean
  }
  variables: Record<string, string>
}

/** 记忆搜索查询参数 */
export interface MemorySearchQuery {
  query: string
  scope_id?: string
  user_id?: string
  num: number
  threshold: number
}

/** 记忆搜索结果 */
export interface MemorySearchResult {
  mem_id: string
  content: string
  memory_type: MemoryType
  score: number
}

/** 平台记忆条目（GET /api/v1/ops/memory 返回，平台全局 SNAKE_CASE，对齐平台 MemoryItem） */
export interface PlatformMemoryItem {
  mem_id: string
  content: string
  type: string
  /** 仅检索结果携带 */
  score?: number
  /** 列表回显：所属 user_id（平台按查询条件回填） */
  user_id?: string
  /** 列表回显：所属 scope_id（平台按查询条件回填） */
  scope_id?: string
  /** 记忆时间戳（ISO 8601，:8516 get_user_mem_by_page 返回） */
  timestamp?: string
  /** 来源消息 id（:8516 get_user_mem_by_page 返回，dreaming 记忆为 session_id） */
  source_id?: string
}

/** 平台记忆列表结果（对齐平台 PageResult<MemoryItem>，snake_case） */
export interface ListMemoriesResult {
  items: PlatformMemoryItem[]
  total: number
  page_idx: number
  page_size: number
}
