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
let inactivityTimer: number | null = null
const INACTIVITY_TIMEOUT = 30 * 60 * 1000 // 30分钟无操作超时

/**
 * 退出登录并跳转到登录页
 */
function logoutAndRedirect(message: string) {
  // 清除登录状态
  localStorage.removeItem('token')
  localStorage.removeItem('username')
  localStorage.removeItem('role')
  localStorage.removeItem('tenantId')
  localStorage.removeItem('scopeIds')
  localStorage.removeItem('permissions')
  
  // 显示错误提示
  ElNotification({
    title: '提示',
    message: message,
    type: 'warning',
    duration: 5000,
  })
  
  // 跳转到登录页
  router.push('/login')
  
  // 停止所有定时器
  if (healthCheckTimer) {
    clearInterval(healthCheckTimer)
    healthCheckTimer = null
  }
  if (inactivityTimer) {
    clearTimeout(inactivityTimer)
    inactivityTimer = null
  }
}

/**
 * 重置无操作计时器
 */
function resetInactivityTimer() {
  if (inactivityTimer) {
    clearTimeout(inactivityTimer)
  }
  inactivityTimer = window.setTimeout(() => {
    logoutAndRedirect('30分钟无操作，已自动退出登录')
  }, INACTIVITY_TIMEOUT)
}

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
    logoutAndRedirect('后端服务已断开，请检查后端是否正常运行（端口 9000）')
  }
}

// 组件挂载时启动定时健康检查和无操作检测
onMounted(() => {
  // 如果不在登录页，启动检测
  if (route.path !== '/login') {
    // 每 30 秒检查一次后端健康状态
    healthCheckTimer = window.setInterval(checkBackendHealth, 30000)
    
    // 启动无操作检测
    resetInactivityTimer()
    
    // 监听用户活动，重置无操作计时器
    const events = ['mousemove', 'mousedown', 'keydown', 'scroll', 'click', 'touchstart']
    events.forEach(event => {
      window.addEventListener(event, resetInactivityTimer, { passive: true })
    })
  }
})

// 组件卸载时清理定时器和事件监听
onUnmounted(() => {
  if (healthCheckTimer) {
    clearInterval(healthCheckTimer)
    healthCheckTimer = null
  }
  if (inactivityTimer) {
    clearTimeout(inactivityTimer)
    inactivityTimer = null
  }
  
  // 移除事件监听
  const events = ['mousemove', 'mousedown', 'keydown', 'scroll', 'click', 'touchstart']
  events.forEach(event => {
    window.removeEventListener(event, resetInactivityTimer)
  })
})
</script>
