/** 配置中心 API — 2026-07-19 P0-3 v3 重构
 *  删除 Dreaming 独立 API，保留内核 Push 模型 + 二次确认
 */
import { api } from './request'
import type {
  KernelConfig,
  UpdateKernelConfigRequest,
  UpdateResult,
  ConfirmTokenIssueRequest,
  ConfirmTokenIssueResponse,
  ConfirmTokenValidateResponse,
} from '@/types/config'

/* ===================== 内核配置（Push 模型） ===================== */

/** 获取内核启动配置（runtime / storage / vector_engine / engine，只读） */
export function getKernelConfig(): Promise<KernelConfig> {
  return api.get<KernelConfig>(`/api/v1/config/kernel`)
}

/** 更新内核配置（Push + 可选 Restart） */
export function updateKernelConfig(data: UpdateKernelConfigRequest): Promise<UpdateResult> {
  return api.put<UpdateResult>(`/api/v1/config/kernel`, data)
}

/* ===================== 二次确认（P0-2） ===================== */

/** 签发二次确认令牌 */
export function issueConfirmToken(
  data: ConfirmTokenIssueRequest
): Promise<ConfirmTokenIssueResponse> {
  return api.post<ConfirmTokenIssueResponse>(`/api/v1/confirm-tokens/issue`, data)
}

/** 校验二次确认令牌 */
export function validateConfirmToken(
  token: string,
  action: string
): Promise<ConfirmTokenValidateResponse> {
  return api.post<ConfirmTokenValidateResponse>(`/api/v1/confirm-tokens/validate`, {
    token,
    action,
  })
}

/** 获取内核重启令牌 */
export function issueKernelConfirmToken(): Promise<{ confirmToken: string }> {
  return api.get<{ confirmToken: string }>(`/api/v1/config/kernel/confirm-token`)
}
