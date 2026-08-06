<template>
  <el-container class="layout-container">
    <el-aside :width="isCollapse ? '64px' : '220px'" class="layout-aside">
      <div class="logo-area">
        <el-icon :size="24" color="#6366F1"><Coin /></el-icon>
        <span v-show="!isCollapse" class="logo-text">记忆管理平台</span>
      </div>
      <el-menu :default-active="activeMenu" :collapse="isCollapse" :collapse-transition="false" router class="side-menu">
        <!-- 平台概览分组 -->

        
        <!-- 记忆系统管理分组 -->
        <el-menu-item-group v-if="userStore.hasPermission('config:read') || userStore.hasPermission('ops:read') || userStore.hasPermission('log:read') || userStore.hasPermission('memory:read')">
          <template #title>记忆系统管理</template>
          <el-menu-item v-if="userStore.hasPermission('config:read')" index="/config">
            <el-icon><HomeFilled /></el-icon>
            <template #title>配置中心</template>
          </el-menu-item>
          <!-- 运维中心：包含记忆列表、记忆检索、任务管理 -->
          <el-menu-item v-if="userStore.hasPermission('ops:read') || userStore.hasPermission('memory:read')" index="/ops">
            <el-icon><Tools /></el-icon>
            <template #title>运维中心</template>
          </el-menu-item>
          <el-menu-item v-if="userStore.hasPermission('log:read')" index="/logs">
            <el-icon><Document /></el-icon>
            <template #title>日志中心</template>
          </el-menu-item>
        </el-menu-item-group>
        
        <!-- 资源管理分组 -->
        <el-menu-item-group v-if="userStore.hasPermission('user:read') || userStore.hasPermission('tenant:read') || userStore.hasPermission('scope:read')">
          <template #title>资源管理</template>
          <el-menu-item v-if="userStore.hasPermission('user:read')" index="/auth">
            <el-icon><User /></el-icon>
            <template #title>权限管理</template>
          </el-menu-item>
          <el-menu-item v-if="userStore.hasPermission('tenant:read') || userStore.hasPermission('scope:read')" index="/tenant">
            <el-icon><OfficeBuilding /></el-icon>
            <template #title>租户管理</template>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="layout-header">
        <div class="header-left"><el-icon class="collapse-btn" :size="20" @click="isCollapse = !isCollapse"><Fold v-if="!isCollapse" /><Expand v-else /></el-icon></div>
        <div class="header-right">
          <el-dropdown @command="handleCommand">
            <span class="user-info"><el-icon><UserFilled /></el-icon><span class="username">{{ userStore.username || '用户' }}</span><el-icon><ArrowDown /></el-icon></span>
            <template #dropdown><el-dropdown-menu><el-dropdown-item command="logout"><el-icon><SwitchButton /></el-icon>退出登录</el-dropdown-item></el-dropdown-menu></template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="layout-main"><router-view /></el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox, ElMessage } from 'element-plus'
import { Coin, HomeFilled, Collection, Setting, Tools, Document, User, OfficeBuilding, Fold, Expand, UserFilled, ArrowDown, SwitchButton } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const isCollapse = ref(false)
const activeMenu = computed(() => route.path)

async function handleCommand(command: string) {
  if (command === 'logout') {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' })
    await userStore.logout()
    ElMessage.success('已退出登录')
    router.push('/login')
  }
}
</script>

<style scoped>
.layout-container { 
  height: 100vh;
  position: relative;
}

/* 科技感背景装饰 */
.layout-container::before {
  content: '';
  position: fixed;
  top: -50%;
  right: -20%;
  width: 600px;
  height: 600px;
  background: radial-gradient(circle, rgba(99, 102, 241, 0.08) 0%, transparent 70%);
  border-radius: 50%;
  pointer-events: none;
  z-index: 0;
  animation: float 20s ease-in-out infinite;
}

.layout-container::after {
  content: '';
  position: fixed;
  bottom: -30%;
  left: -10%;
  width: 500px;
  height: 500px;
  background: radial-gradient(circle, rgba(99, 102, 241, 0.06) 0%, transparent 70%);
  border-radius: 50%;
  pointer-events: none;
  z-index: 0;
  animation: float 25s ease-in-out infinite reverse;
}

@keyframes float {
  0%, 100% { transform: translate(0, 0) scale(1); }
  50% { transform: translate(30px, -30px) scale(1.1); }
}

.layout-aside { 
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(20px);
  border-right: 1px solid rgba(229, 231, 235, 0.8);
  transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
  box-shadow: 
    4px 0 20px rgba(99, 102, 241, 0.05),
    2px 0 10px rgba(99, 102, 241, 0.03);
  position: relative;
  z-index: 1;
}

.logo-area { 
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 20px;
  overflow: hidden;
  white-space: nowrap;
  border-bottom: 1px solid rgba(229, 231, 235, 0.8);
  position: relative;
}

/* Logo 呼吸光效 */
.logo-area::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(99, 102, 241, 0.5), transparent);
  animation: shimmer 3s ease-in-out infinite;
}

@keyframes shimmer {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}

.logo-text { 
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
  background: linear-gradient(135deg, #1F2937 0%, #6366F1 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.side-menu {
  border-right: none !important;
  background-color: #FFFFFF !important;
}

/* MemOS 风格：分组标题样式 */
.side-menu :deep(.el-menu-item-group__title) {
  color: #6B7280 !important;
  font-size: 15px !important;
  font-weight: 600 !important;
  padding: 20px 20px 8px 20px !important;
  letter-spacing: 0.3px !important;
  pointer-events: none !important;
  line-height: 1.4 !important;
}

.side-menu :deep(.el-menu-item) {
  padding-left: 20px !important;
}

.side-menu :deep(.el-menu-item-group) {
  margin-bottom: 8px;
}

.layout-aside :deep(.el-menu) { 
  border-right: none; 
}

.layout-header { 
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(20px);
  border-bottom: 1px solid rgba(229, 231, 235, 0.8);
  box-shadow: 
    0 4px 6px -1px rgba(99, 102, 241, 0.05),
    0 2px 4px -1px rgba(99, 102, 241, 0.03);
  height: 60px;
  position: relative;
  z-index: 1;
}

/* 顶部栏光泽效果 */
.layout-header::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(99, 102, 241, 0.3), transparent);
}

.collapse-btn { 
  cursor: pointer; 
  color: #6B7280; 
}

.collapse-btn:hover { 
  color: #6366F1; 
}

.user-info { 
  display: flex; 
  align-items: center; 
  gap: 6px; 
  cursor: pointer; 
  color: #6B7280; 
}

.user-info:hover { 
  color: #6366F1; 
}

.username { 
  font-size: 14px; 
}

.layout-main { 
  background: transparent;
  padding: 20px;
  overflow-y: auto;
  position: relative;
  z-index: 1;
}
</style>
