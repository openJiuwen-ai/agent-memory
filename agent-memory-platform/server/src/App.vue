<!--
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
-->
<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElNotification } from 'element-plus'
import { api } from '@/api/request'

const router = useRouter()
const route = useRoute()
let healthCheckTimer: number | null = null

/**
 * 检查后端服务健康状态
 * 如果后端断开，清除登录状态并跳转到登录页
 */
async function checkBackendHealth() {
  // 如果当前在登录页，不需要检查
  if (route.path === '/login') {
    return
  }

  try {
    await api.get('/api/v1/ops/health-probe')
    // 后端正常，不做任何操作
  } catch (error) {
    // 后端断开或无响应
    console.error('后端服务连接失败:', error)
    
    // 清除登录状态
    localStorage.removeItem('token')
    localStorage.removeItem('username')
    localStorage.removeItem('role')
    localStorage.removeItem('tenantId')
    localStorage.removeItem('scopeIds')
    localStorage.removeItem('permissions')
    
    // 显示错误提示
    ElNotification({
      title: '后端服务已断开',
      message: '无法连接到后端服务，请检查后端是否正常运行（端口 9000）',
      type: 'error',
      duration: 5000,
    })
    
    // 跳转到登录页
    router.push('/login')
    
    // 停止定时检查
    if (healthCheckTimer) {
      clearInterval(healthCheckTimer)
      healthCheckTimer = null
    }
  }
}

// 组件挂载时启动定时健康检查
onMounted(() => {
  // 每 30 秒检查一次后端健康状态
  healthCheckTimer = window.setInterval(checkBackendHealth, 30000)
})

// 组件卸载时清理定时器
onUnmounted(() => {
  if (healthCheckTimer) {
    clearInterval(healthCheckTimer)
    healthCheckTimer = null
  }
})
</script>
