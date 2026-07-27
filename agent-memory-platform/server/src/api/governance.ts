import request from '@/api/request'

// 集成架构：前端 → 平台(platform, /api/v1/ops/governance/*) → 记忆系统(:8516)。
// 治理运维层只做：保留策略(TTL 清理) + 配额 + 策略落库。去重/合并/合规护栏属引擎职责。

/** 治理策略：lifecycle(TTL) + quota(上限)。 */
export function getGovernanceStrategy(): Promise<any> {
  return request.get('/api/v1/ops/governance/strategy').then((r: any) => r)
}

/** 保存治理策略。 */
export function saveGovernanceStrategy(strategy: any): Promise<void> {
  return request.put('/api/v1/ops/governance/strategy', strategy).then(() => undefined)
}

/** 保留清理：dry_run=true 预览过期记忆，false 真删。 */
export function runGovernanceCleanup(userId?: string, scopeId?: string, dryRun = true): Promise<any> {
  return request
    .post('/api/v1/ops/governance/cleanup', { dry_run: dryRun }, {
      params: { user_id: userId || undefined, scope_id: scopeId || undefined },
    })
    .then((r: any) => r)
}

/** 治理页聚合 bundle：governance_summary + quota_status + retention。 */
export function getGovernancePage(userId?: string, scopeId?: string): Promise<any> {
  return request
    .get('/api/v1/ui/governance/page', {
      params: { user_id: userId || undefined, scope_id: scopeId || undefined },
    })
    .then((r: any) => r)
}
