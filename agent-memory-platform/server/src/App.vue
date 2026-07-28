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
import { useRouter } from 'vue-router'
import { ElNotification } from 'element-plus'
import { useUserStore } from '@/stores/user'

const router = useRouter()
let ws: WebSocket | null = null
let wsReconnectTimer: number | null = null
let inactivityTimer: number | null = null
let lastHeartbeatTime: number = 0
let wsConnected: boolean = false // 新增：标记WebSocket是否已连接
const HEARTBEAT_TIMEOUT = 15000 // 15 秒未收到心跳判定为断开
const INACTIVITY_TIMEOUT = 30 * 60 * 1000 // 30 分钟无操作超时
const WS_RECONNECT_DELAY = 3000 // 断线重连延迟 3 秒

/**
 * 退出登录并跳转到登录页
 */
function logoutAndRedirect(message: string) {
  console.log('[退出登录] 开始执行 - message:', message)
  console.log('[退出登录] 当前路由:', router.currentRoute.value.path)
  
  // 清除登录状态（localStorage）
  localStorage.removeItem('token')
  localStorage.removeItem('username')
  localStorage.removeItem('role')
  localStorage.removeItem('tenantId')
  localStorage.removeItem('scopeIds')
  localStorage.removeItem('permissions')
  
  // 清除 Pinia store 状态（关键！否则路由守卫会拦截跳转）
  const userStore = useUserStore()
  userStore.token = ''
  userStore.username = ''
  userStore.role = ''
  userStore.tenantId = ''
  userStore.scopeIds = []
  userStore.permissions = []
  console.log('[退出登录] 已清除 userStore，isLoggedIn:', userStore.isLoggedIn)
  
  // 显示错误提示
  ElNotification({
    title: '提示',
    message: message,
    type: 'warning',
    duration: 5000,
  })
  
  // 停止所有定时器（先停止，避免重复触发）
  disconnectWebSocket()
  if (inactivityTimer) {
    clearTimeout(inactivityTimer)
    inactivityTimer = null
  }
  
  // 跳转到登录页（延迟 100ms 确保清理完成）
  console.log('[退出登录] 准备跳转到 /login')
  setTimeout(() => {
    console.log('[退出登录] 执行 router.push("/login")')
    router.push('/login')
  }, 100)
}

/**
 * 断开 WebSocket 连接
 */
function disconnectWebSocket() {
  if (ws) {
    console.log('[WebSocket] 主动断开连接')
    ws.close()
    ws = null
  }
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer)
    wsReconnectTimer = null
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
 * 建立 WebSocket 心跳连接
 */
function connectWebSocket() {
  if (ws) {
    console.log('[WebSocket] 连接已存在，跳过')
    return
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${window.location.host}/ws/heartbeat`
  console.log('[WebSocket] 尝试建立连接:', wsUrl)

  ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    console.log('[WebSocket] ✅ 连接成功')
    wsConnected = true
    lastHeartbeatTime = Date.now()
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      if (data.type === 'heartbeat') {
        lastHeartbeatTime = Date.now()
        console.log('[WebSocket] 💓 收到心跳 -', data.timestamp)
      }
    } catch (e) {
      console.warn('[WebSocket] 解析消息失败:', e)
    }
  }

  ws.onerror = (error) => {
    console.error('[WebSocket] ❌ 连接错误:', error)
  }

  ws.onclose = (event) => {
    console.log('[WebSocket] 连接已关闭 - code:', event.code, 'reason:', event.reason)
    ws = null
    wsConnected = false
    
    // WebSocket 断开（非主动关闭）立即判定后端断开
    if (router.currentRoute.value.path !== '/login') {
      // code 1006 = 异常关闭（后端断开），1000 = 正常关闭
      if (event.code === 1006 || event.code === 1005 || event.code === 1011) {
        console.error('[WebSocket] 后端异常断开，立即退出登录')
        logoutAndRedirect('后端服务已断开，请检查后端是否正常运行')
        return
      }
      
      // 其他情况：3 秒后尝试重连
      if (!wsReconnectTimer) {
        console.log('[WebSocket] 3 秒后尝试重连...')
        wsReconnectTimer = window.setTimeout(() => {
          wsReconnectTimer = null
          if (router.currentRoute.value.path !== '/login') {
            connectWebSocket()
          }
        }, WS_RECONNECT_DELAY)
      }
    }
  }
}

/**
 * 心跳监控：每 5 秒检查一次是否收到心跳
 */
function startHeartbeatMonitor() {
  const monitorTimer = window.setInterval(() => {
    if (router.currentRoute.value.path === '/login') {
      clearInterval(monitorTimer)
      return
    }
    
    // 只有在WebSocket已连接过且超时才判定断开
    if (wsConnected && lastHeartbeatTime > 0) {
      const timeSinceLastHeartbeat = Date.now() - lastHeartbeatTime
      if (timeSinceLastHeartbeat > HEARTBEAT_TIMEOUT) {
        console.error('[心跳监控]  超时未收到心跳，判定后端断开')
        clearInterval(monitorTimer)
        logoutAndRedirect('后端服务已断开，请检查后端是否正常运行')
      }
    }
  }, 5000)
}

// 组件挂载时启动 WebSocket 心跳监控
onMounted(() => {
  const currentPath = router.currentRoute.value.path
  console.log('[App 启动] 当前路径:', currentPath)
  
  // 如果不在登录页，启动 WebSocket 心跳监控
  if (currentPath !== '/login') {
    console.log('[WebSocket] ✅ 启动心跳监控')
    connectWebSocket()
    startHeartbeatMonitor()
    
    // 启动无操作检测
    resetInactivityTimer()
    
    // 监听用户活动，重置无操作计时器
    const events = ['mousemove', 'mousedown', 'keydown', 'scroll', 'click', 'touchstart']
    events.forEach(event => {
      window.addEventListener(event, resetInactivityTimer, { passive: true })
    })
  } else {
    console.log('[App 启动] 当前在登录页，跳过心跳监控')
  }
  
  // 监听路由变化
  router.afterEach((to, from) => {
    console.log('[路由变化] 从', from.path, '到', to.path)
    
    // 如果跳转到登录页，断开 WebSocket
    if (to.path === '/login') {
      disconnectWebSocket()
    } 
    // 如果从登录页跳转到其他页面，重新建立连接
    else if (from.path === '/login' && to.path !== '/login') {
      console.log('[WebSocket] ✅ 从登录页跳转，重新启动心跳监控')
      connectWebSocket()
      startHeartbeatMonitor()
    }
  })
})

// 组件卸载时清理定时器和事件监听
onUnmounted(() => {
  console.log('[App 卸载] 清理所有资源')
  
  // 断开 WebSocket
  disconnectWebSocket()
  
  // 停止无操作定时器
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
