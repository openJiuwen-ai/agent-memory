<template>
  <div class="id-mapping">
    <el-card shadow="never">
      <template #header>
        <span>内核ID映射关系</span>
      </template>
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 16px">
        <template #title>
          内核使用 <code>tenant_id__scope_id</code> 和 <code>tenant_id__user_id</code> 格式进行多租户隔离
        </template>
      </el-alert>

      <el-table :data="mappings" border>
        <el-table-column prop="tenant_id" label="租户ID" width="140" />
        <el-table-column prop="user_id" label="用户ID" width="140" />
        <el-table-column prop="scope_id" label="Scope ID" width="160" />
        <el-table-column label="内核 user_id" min-width="200">
          <template #default="{ row }">
            <code>{{ row.kernel_user_id }}</code>
          </template>
        </el-table-column>
        <el-table-column label="内核 scope_id" min-width="200">
          <template #default="{ row }">
            <code>{{ row.kernel_scope_id }}</code>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const mappings = ref([
  {
    tenant_id: 'tenant_001',
    user_id: 'user_001',
    scope_id: 'chat_session_1',
    kernel_user_id: 'tenant_001__user_001',
    kernel_scope_id: 'tenant_001__chat_session_1',
  },
  {
    tenant_id: 'tenant_001',
    user_id: 'user_002',
    scope_id: 'customer_service',
    kernel_user_id: 'tenant_001__user_002',
    kernel_scope_id: 'tenant_001__customer_service',
  },
  {
    tenant_id: 'tenant_002',
    user_id: 'user_003',
    scope_id: 'default_scope',
    kernel_user_id: 'tenant_002__user_003',
    kernel_scope_id: 'tenant_002__default_scope',
  },
])
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
}
code {
  background: #f5f7fa;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
  color: #409eff;
}
</style>
