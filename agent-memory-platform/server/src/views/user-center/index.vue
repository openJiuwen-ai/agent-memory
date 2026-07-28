<template>
  <div class="user-center">
    <el-tabs v-model="activeTab" type="border-card">
      <el-tab-pane label="用户列表" name="users">
        <UserList />
      </el-tab-pane>
      <el-tab-pane label="角色管理" name="roles">
        <RoleMatrix />
      </el-tab-pane>
      <el-tab-pane label="内核ID映射" name="mapping">
        <IdMapping />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import UserList from './UserList.vue'
import RoleMatrix from './RoleMatrix.vue'
import IdMapping from './IdMapping.vue'

const activeTab = ref('users')
</script>
