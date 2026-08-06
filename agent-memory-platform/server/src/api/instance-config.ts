/** 实例级配置 API — 2026-07-17 P0-3 v2
 *  单例（id=1），对应 INSTANCE 类型模板的应用结果
 *  端点 /api/v1/instance-config
 */
import { api } from './request'
import type { InstanceConfig } from '@/types/config'

/** 获取实例级配置（单例） */
export function getInstanceConfig(): Promise<InstanceConfig> {
  return api.get<InstanceConfig>(`/api/v1/instance-config`)
}

/** 更新实例级配置（自动 version +1） */
export function updateInstanceConfig(configJson: string): Promise<InstanceConfig> {
  return api.put<InstanceConfig>(`/api/v1/instance-config`, { configJson })
}
