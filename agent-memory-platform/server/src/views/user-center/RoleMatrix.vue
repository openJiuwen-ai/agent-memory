<template>
  <div class="role-matrix">
    <el-card shadow="never">
      <template #header>角色权限矩阵</template>
      <el-table :data="roleData" border>
        <el-table-column prop="permission" label="权限点" width="180" />
        <el-table-column prop="description" label="说明" width="160" />
        <el-table-column v-for="role in roles" :key="role" :label="role" align="center" width="110">
          <template #default="{ row }">
            <el-tag v-if="row[role]" type="success" size="small">✓</el-tag>
            <span v-else class="text-muted">✗</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ROLE_PERMISSIONS } from '@/api/permissions'
import type { UserRole } from '@/types/tenant'

const roles: UserRole[] = ['SUPER_ADMIN', 'PLATFORM_ADMIN', 'SECURITY_ADMIN', 'SCOPE_ADMIN', 'READ_ONLY', 'VIEWER']

const roleData = [
  { perm: 'tenant:read', desc: '租户查看' },
  { perm: 'tenant:write', desc: '租户管理' },
  { perm: 'user:read', desc: '用户查看' },
  { perm: 'user:write', desc: '用户管理' },
  { perm: 'config:read', desc: '配置查看' },
  { perm: 'config:write', desc: '配置修改' },
  { perm: 'ops:read', desc: '运维查看' },
  { perm: 'ops:write', desc: '运维执行' },
  { perm: 'memory:read', desc: '记忆浏览' },
  { perm: 'memory:write', desc: '记忆编辑' },
  { perm: 'memory:delete', desc: '记忆删除' },
  { perm: 'log:read', desc: '日志查看' },
  { perm: 'trace:read', desc: '追踪查看' },
  { perm: 'template:read', desc: '模板查看' },
  { perm: 'template:write', desc: '模板管理' },
  { perm: 'scope:read', desc: 'Scope 查看' },
  { perm: 'scope:write', desc: 'Scope 管理' },
].map(item => ({
  permission: item.perm,
  description: item.desc,
  SUPER_ADMIN: ROLE_PERMISSIONS.SUPER_ADMIN.includes(item.perm as any),
  PLATFORM_ADMIN: ROLE_PERMISSIONS.PLATFORM_ADMIN.includes(item.perm as any),
  SECURITY_ADMIN: ROLE_PERMISSIONS.SECURITY_ADMIN.includes(item.perm as any),
  SCOPE_ADMIN: ROLE_PERMISSIONS.SCOPE_ADMIN.includes(item.perm as any),
  READ_ONLY: ROLE_PERMISSIONS.READ_ONLY.includes(item.perm as any),
  VIEWER: ROLE_PERMISSIONS.VIEWER.includes(item.perm as any),
}))
</script>

<style scoped>
.text-muted { color: #c0c4cc; }
</style>
