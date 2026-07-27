/** 租户管理相关类型定义 */

/** 用户角色（基于 730 详细设计 §1.5 RBAC 权限模型） */
export type UserRole = 'SUPER_ADMIN' | 'PLATFORM_ADMIN' | 'SECURITY_ADMIN' | 'SCOPE_ADMIN' | 'READ_ONLY' | 'VIEWER'

/** 租户（组织容器） */
export interface Tenant {
  id: string
  name: string
  status: string  // active | disabled
  remark: string
  scopeIds?: string[]  // 已分配的Scope ID列表
  createTime: string
  updateTime: string
  /** 扩展字段（2026-07-17 适配前端组件） */
  adminName?: string
  adminEmail?: string
  scopeCount?: number
  userCount?: number
  description?: string
  /** 当前生效的 SCOPE 模板信息（应用模板时展示，避免覆盖误操作） */
  currentTemplateId?: string
  currentTemplateName?: string
}

/** 账号（系统管理员 / 租户管理员 / 租户普通用户 / 访客） */
export interface TenantAccount {
  id: string
  /** 所属租户ID，系统管理员账号为空字符串 */
  tenantId: string
  username: string
  /** mock 阶段明文存储，仅用于前端演示 */
  password: string
  role: UserRole
  scopeIds: string[]
  remark: string
  createTime: string
  updateTime: string
}

/** 租户列表展示项（聚合了管理员信息与成员数量） */
export interface TenantListItem extends Tenant {
  adminUsername: string
  adminScopeIds: string[]
  memberCount: number
}

/** 租户列表查询参数 */
export interface TenantListQuery {
  keyword?: string
  page: number
  pageSize: number
}

export interface TenantListResult {
  list: TenantListItem[]
  total: number
}

/** 创建租户 */
export interface TenantCreateForm {
  name: string
  remark: string
  scopeIds?: string[]  // 分配的Scope ID列表
}

/** 修改租户：名称不可改；密码可选修改 */
export interface TenantUpdateForm {
  id: string
  remark: string
  scopeIds?: string[]  // 新的Scope ID列表
  adminScopeIds: string[]
  /** 是否修改管理员密码 */
  changePassword: boolean
  /** 操作者不是系统管理员时必填 */
  oldPassword?: string
  newPassword?: string
}

/** 租户详情（含管理员账号信息） */
export interface TenantDetail extends Tenant {
  admin: TenantAccount
}

/** 租户成员（普通用户 / 访客）表单 */
export interface TenantMemberForm {
  id?: string
  tenantId: string
  username: string
  /** 新增时必填，修改时可选（留空表示不改密码） */
  password?: string
  role: 'READ_ONLY' | 'SCOPE_ADMIN'
  scopeIds: string[]
  remark: string
}

/** 租户成员（API 响应体，2026-07-17 新增） */
export interface TenantMember {
  id: string
  tenantId: string
  username: string
  role: UserRole
  scopeIds: string[]
  remark: string
  createTime: string
  updateTime: string
}
