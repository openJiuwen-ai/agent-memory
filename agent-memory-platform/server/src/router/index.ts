import { createRouter, createWebHistory, type NavigationGuardNext, type RouteLocationNormalized } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import type { Permission } from '@/types/permission'

const routes = [
  { path: '/login', name: 'Login', component: () => import('@/views/login/index.vue'), meta: { requiresAuth: false, title: '登录' } },
  {
    path: '/', component: () => import('@/layouts/BasicLayout.vue'), redirect: '/ops', meta: { requiresAuth: true },
    children: [
      { path: 'dashboard', name: 'Dashboard', component: () => import('@/views/dashboard/index.vue'), meta: { title: '首页' } },
      { path: 'memory', name: 'Memory', component: () => import('@/views/memory/index.vue'), meta: { title: '记忆管理', permission: 'memory:read' } },

      { path: 'config', name: 'Config', component: () => import('@/views/config-center/index.vue'), meta: { title: '配置中心', permission: 'config:read' } },
      { path: 'config/templates/new', name: 'ConfigTemplateCreate', component: () => import('@/views/config-center/TemplateDetailPage.vue'), meta: { title: '新建模板', permission: 'config:read' } },
      { path: 'config/templates/:id', name: 'ConfigTemplateDetail', component: () => import('@/views/config-center/TemplateDetailPage.vue'), meta: { title: '模板详情', permission: 'config:read' } },
      { path: 'config', name: 'Config', component: () => import('@/views/config-center/index.vue'), meta: { title: '配置中心' } },
      // 运维中心（包含子页面：记忆列表、记忆检索、任务管理）
      {
        path: 'ops', name: 'Ops', component: () => import('@/views/ops-center/index.vue'), meta: { title: '运维中心' },
        redirect: '/ops/memory-list',
        children: [
          { path: 'memory-list', name: 'OpsMemoryList', component: () => import('@/views/ops-center/MemoryList.vue'), meta: { title: '记忆列表', permission: 'memory:read' } },
          // { path: 'governance', name: 'OpsGovernance', component: () => import('@/views/ops-center/Governance.vue'), meta: { title: '记忆治理', permission: 'governance:read' } },  // 暂时隐藏
          // { path: 'trace', name: 'OpsTrace', component: () => import('@/views/ops-center/Trace.vue'), meta: { title: '记忆追溯', permission: 'trace:read' } },  // 已合并到记忆列表详情
          { path: 'tasks', name: 'OpsTasks', component: () => import('@/views/ops-center/Tasks.vue'), meta: { title: '任务管理', permission: 'ops:read' } },
          // { path: 'commands', name: 'OpsCommands', component: () => import('@/views/ops-center/Commands.vue'), meta: { title: '远程命令', permission: 'ops:read' } },  // 暂时隐藏
          { path: 'search', name: 'OpsSearch', component: () => import('@/views/ops-center/Search.vue'), meta: { title: '记忆检索', permission: 'memory:read' } },
          // { path: 'statistics', name: 'OpsStatistics', component: () => import('@/views/ops-center/MemoryStatistics.vue'), meta: { title: '记忆统计' } },  // 暂时隐藏
        ],
      },
      { path: 'logs', name: 'Logs', component: () => import('@/views/log-center/index.vue'), meta: { title: '日志中心', permission: 'log:read' } },
      // 权限管理（包含用户+角色）
      {
        path: 'auth', name: 'Auth', component: () => import('@/views/auth/index.vue'), meta: { title: '权限管理', permission: 'user:read' },
        redirect: '/auth/users',
        children: [
          { path: 'users', name: 'AuthUsers', component: () => import('@/views/user-center/UserList.vue'), meta: { title: '用户' } },
          { path: 'roles', name: 'AuthRoles', component: () => import('@/views/user-center/RoleMatrix.vue'), meta: { title: '角色' } },
        ],
      },
      { path: 'tenant', name: 'Tenant', component: () => import('@/views/tenant-center/index.vue'), meta: { title: '租户管理' } },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to: RouteLocationNormalized, _from: RouteLocationNormalized, next: NavigationGuardNext) => {
  const userStore = useUserStore()
  document.title = to.meta.title ? `${to.meta.title} - 记忆管理平台` : '记忆管理平台'

  if (!to.meta.requiresAuth) {
    if (to.path === '/login' && userStore.isLoggedIn) next('/ops')
    else next()
  } else {
    if (!userStore.isLoggedIn) { next('/login'); return }
    if (to.meta.permission && !userStore.hasPermission(to.meta.permission as Permission)) {
      ElMessage.warning('当前账号无权访问该页面')
      next('/dashboard')
      return
    }
    next()
  }
})

export default router
