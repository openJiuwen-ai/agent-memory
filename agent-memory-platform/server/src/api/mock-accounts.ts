import type { TenantAccount } from '@/types/tenant'

// TODO: 后端账号体系就绪后，将本文件替换为真实的用户/租户后端接口调用。
// 当前作为 mock 阶段的“唯一账号数据源”，被 api/auth.ts（登录鉴权）与
// api/tenant.ts（租户管理）共同引用，避免数据不一致。

function now(): string {
  return new Date().toISOString().replace('T', ' ').substring(0, 19)
}

export function genAccountId(prefix = 'acc'): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1000)}`
}

/** 内置系统管理员账号（唯一），其余账号均通过租户管理模块创建 */
export let accounts: TenantAccount[] = [
  {
    id: genAccountId('sysadmin'),
    tenantId: '',
    username: 'admin',
    password: 'admin123',
    role: 'SUPER_ADMIN',
    scopeIds: [],
    remark: '系统管理员（内置账号）',
    createTime: now(),
    updateTime: now(),
  },
]

export function setAccounts(next: TenantAccount[]): void {
  accounts = next
}

export function findAccountByUsername(username: string): TenantAccount | undefined {
  return accounts.find((a) => a.username === username)
}
