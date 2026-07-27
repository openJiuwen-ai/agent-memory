/**
 * 日志中心 API — 2026-07-21 v5 异步重构
 *
 * 对齐 §6.3.2 + §6.4（运行日志不入库 + 异步三段式 + 先查询后下载）：
 *  - 运行日志（内核，不入库）：GET /logs/runtime/tail + /logs/runtime/files + /logs/runtime/download?filename=
 *    （运行日志不提供日志级别动态管理功能 §6.3.2，已删除 PUT /logs/runtime/level）
 *  - 操作审计日志（服务层 DB）：GET /logs/operations + /logs/operations/export
 *  - 消息日志 L1（内核文件）：GET /logs/messages/tail（下载统一走 /logs/runtime/download?filename=）
 *  - 消息日志 L2（服务层 DB）：GET /logs/messages + /logs/messages/export
 *  - 一键采集（异步三段式）：POST /logs/collect + GET /logs/collect/{id} 轮询 + GET /logs/collect/{id}/download
 */
import { api } from './request'

// ==================== 运行日志（内核，不入库） ====================

export interface RuntimeTailResult {
  lines: string[]
  total: number
  error?: string
}

/** 运行日志来源：kernel=内核（默认），platform=服务层自身 platform.log/access log */
export type RuntimeLogSource = 'kernel' | 'platform'

export function tailRuntimeLogs(params: {
  lines?: number
  level?: string
  event_type?: string
  source?: RuntimeLogSource
}): Promise<RuntimeTailResult> {
  return api.get<RuntimeTailResult>('/api/v1/logs/runtime/tail', { params })
}

/**
 * 按文件名下载内核运行日志（先查询后下载模式）。
 * 前端先调 listRuntimeLogFiles() 获取文件列表，用户选择具体文件后调此接口下载。
 *
 * @param filename 日志文件相对路径（由 listRuntimeLogFiles 返回的 filename 字段，如 run/jiuwen.log）
 */
export function downloadRuntimeLogs(
  filename: string,
  source?: RuntimeLogSource,
): Promise<Blob> {
  return api.get('/api/v1/logs/runtime/download', {
    params: { filename, source },
    responseType: 'blob',
  })
}

/** 内核日志文件项（先查询后下载模式，由内核 /logs/files 返回） */
export interface RuntimeLogFileItem {
  filename: string
  log_type: string
  size_bytes: number
  size_human: string
  created_at: string
  modified_at: string
  is_rotated: boolean
}

/** 列出可下载的内核运行日志文件项（调内核 /logs/files，可按日期范围过滤） */
export function listRuntimeLogFiles(params?: {
  start_date?: string
  end_date?: string
  source?: RuntimeLogSource
}): Promise<RuntimeLogFileItem[]> {
  return api.get<RuntimeLogFileItem[]>('/api/v1/logs/runtime/files', { params })
}

// ==================== 操作审计日志（服务层 DB） ====================

export interface OperationLogRow {
  id: string
  admin_user_id: string
  operator_id: string
  operation_type: string
  request_method: string | null
  request_path: string | null
  request_body: string | null
  response_status: number | null
  error_message: string | null
  duration_ms: number | null
  operated_at: string
}

export interface OperationLogPage {
  records: OperationLogRow[]
  total: number
  current: number
  size: number
}

export function queryOperationLogs(params: {
  admin_user_id?: string
  operator_id?: string
  type?: string
  success_only?: boolean
  start?: string
  end?: string
  page?: number
  size?: number
}): Promise<OperationLogPage> {
  return api.get<OperationLogPage>('/api/v1/logs/operations', { params })
}

export function exportOperationLogs(params: {
  admin_user_id?: string
  operator_id?: string
  type?: string
  success_only?: boolean
  start: string
  end: string
}): Promise<Blob> {
  return api.get('/api/v1/logs/operations/export', {
    params,
    responseType: 'blob',
  })
}

// ==================== 消息日志 L1（内核文件，不入库） ====================

export function tailMessageLogs(params: {
  lines?: number
  level?: string
}): Promise<RuntimeTailResult> {
  return api.get<RuntimeTailResult>('/api/v1/logs/messages/tail', { params })
}

/**
 * 下载消息日志 L1 文件（统一走运行日志下载通道）。
 * 消息日志 L1 文件即内核日志文件的一种（event_type=message），
 * 下载统一由 RuntimeLogController /download?filename= 管理（先查询后下载模式）。
 * 前端先调 listRuntimeLogFiles() 获取文件列表，选择文件后调 downloadRuntimeLogs(filename) 下载。
 */
export function downloadMessageLogs(filename: string): Promise<Blob> {
  return downloadRuntimeLogs(filename)
}

// ==================== 消息日志 L2（服务层 DB） ====================

export interface MessageLogRow {
  id: string
  request_id: string
  admin_user_id: string
  user_id: string | null
  scope_name: string | null
  api_path: string | null
  api_method: string | null
  message_count: number | null
  error_message: string | null
  created_at: string
}

export interface MessageLogPage {
  records: MessageLogRow[]
  total: number
  current: number
  size: number
}

export function queryMessageLogs(params: {
  admin_user_id?: string
  user_id?: string
  scope_name?: string
  success_only?: boolean
  start?: string
  end?: string
  page?: number
  size?: number
}): Promise<MessageLogPage> {
  return api.get<MessageLogPage>('/api/v1/logs/messages', { params })
}

export function exportMessageLogs(params: {
  admin_user_id?: string
  user_id?: string
  scope_name?: string
  success_only?: boolean
  start: string
  end: string
}): Promise<Blob> {
  return api.get('/api/v1/logs/messages/export', {
    params,
    responseType: 'blob',
  })
}

// ==================== 一键采集（持久化） ====================

export interface CollectRecord {
  id: string
  scene: string
  name: string
  start_date: string
  end_date: string
  tenant_id: string | null
  file_path: string
  file_size: number | null
  status: string
  operator_id: string | null
  created_at: string
  remark: string | null
}

/**
 * 触发一键采集（异步三段式第一步）。
 * POST 下发后立即返回 status=COLLECTING，后台线程异步打包。
 * 前端需轮询 getCollectRecord(id) 直到 status 变为 READY/FAILED。
 */
export function collectLogs(params: {
  scene: string
  start_date: string
  end_date: string
  admin_user_id?: string
  operator_id?: string
  remark?: string
}): Promise<CollectRecord> {
  return api.post<CollectRecord>('/api/v1/logs/collect', null, { params })
}

/** 查询单条采集记录状态（异步三段式第二步：轮询） */
export function getCollectRecord(id: string): Promise<CollectRecord> {
  return api.get<CollectRecord>(`/api/v1/logs/collect/${id}`)
}

/** 列出采集记录（按创建时间倒序） */
export function listCollectRecords(params?: { scene?: string; limit?: number }): Promise<CollectRecord[]> {
  return api.get<CollectRecord[]>('/api/v1/logs/collect', { params })
}

/** 下载某个采集包（异步三段式第三步：status=READY 后可下载，返回 blob） */
export function downloadCollectRecord(id: string): Promise<Blob> {
  return api.get(`/api/v1/logs/collect/${id}/download`, { responseType: 'blob' })
}

/** 删除某个采集包（文件+记录） */
export function deleteCollectRecord(id: string): Promise<void> {
  return api.delete(`/api/v1/logs/collect/${id}`)
}
