/** 权限点定义（基于 V3-记忆系统服务化设计-v4.0 §权限模型） */

/** 17 个权限点 */
export type Permission =
  // 租户管理
  | 'tenant:read'
  | 'tenant:write'
  // 用户管理
  | 'user:read'
  | 'user:write'
  // 配置管理
  | 'config:read'
  | 'config:write'
  // 运维管理
  | 'ops:read'
  | 'ops:write'
  // 记忆管理
  | 'memory:read'
  | 'memory:write'
  | 'memory:delete'
  // 日志查看
  | 'log:read'
  // 记忆追溯
  | 'trace:read'
  // 模板管理
  | 'template:read'
  | 'template:write'
  // Scope 管理
  | 'scope:read'
  | 'scope:write'
