<template>
  <div class="tenant-center">
    <el-tabs v-model="activeTab" type="border-card" class="tenant-tabs">
      <!-- 租户列表（需要 tenant:read 权限） -->
      <el-tab-pane v-if="userStore.hasPermission('tenant:read')" label="租户列表" name="tenants">
        <TenantList />
      </el-tab-pane>
      <!-- Scope管理（需要 tenant:read 或 scope:read 权限） -->
      <el-tab-pane v-if="userStore.hasPermission('tenant:read') || userStore.hasPermission('scope:read')" label="Scope管理" name="scopes">
        <ScopeManagement />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useUserStore } from '@/stores/user'
import TenantList from './TenantList.vue'
import ScopeManagement from './ScopeManagement.vue'

const userStore = useUserStore()

// 根据用户权限自动选择默认标签页
function getDefaultTab(): string {
  // 如果有 tenant:read 权限，默认显示租户列表
  if (userStore.hasPermission('tenant:read')) {
    return 'tenants'
  }
  // 否则如果有 scope:read 权限，默认显示Scope管理
  if (userStore.hasPermission('scope:read')) {
    return 'scopes'
  }
  // 都没有权限，默认显示租户列表（会被 v-if 隐藏）
  return 'tenants'
}

const activeTab = ref(getDefaultTab())
</script>

<style scoped>
.tenant-center {
  padding: 0;
}

.tenant-tabs {
  background: #FFFFFF;
  border-radius: 12px;
  overflow: hidden;
}

.tenant-tabs :deep(.el-tabs__content) {
  padding: 20px;
}
</style>
