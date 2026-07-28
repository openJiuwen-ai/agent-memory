<template>
  <div class="ops-center">
    <el-tabs v-model="activeTab" type="border-card" @tab-change="onTabChange">
      <!-- 记忆列表（需要 memory:read 权限） -->
      <el-tab-pane v-if="userStore.hasPermission('memory:read')" label="记忆列表" name="memory-list">
        <MemoryList />
      </el-tab-pane>

      <!-- 记忆检索（需要 memory:read 权限） -->
      <el-tab-pane v-if="userStore.hasPermission('memory:read')" label="记忆检索" name="search">
        <Search />
      </el-tab-pane>

      <!-- 任务管理（需要 ops:read 权限） -->
      <el-tab-pane v-if="userStore.hasPermission('ops:read')" label="任务管理" name="tasks">
        <Tasks />
      </el-tab-pane>

      <!-- 远程命令（暂时隐藏）
      <el-tab-pane label="远程命令" name="commands">
        <Commands />
      </el-tab-pane>
      -->
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import MemoryList from './MemoryList.vue'
import Search from './Search.vue'
import Tasks from './Tasks.vue'
// import Commands from './Commands.vue'  // 暂时隐藏

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

// tab name <-> /ops 子路径段，二者同名
const TAB_NAMES = ['memory-list', 'search', 'tasks']

function currentTabFromPath(): string {
  const seg = route.path.split('/').pop() || ''
  return TAB_NAMES.includes(seg) ? seg : 'memory-list'
}

const activeTab = ref(currentTabFromPath())

watch(
  () => route.path,
  () => {
    const t = currentTabFromPath()
    if (t !== activeTab.value) activeTab.value = t
  }
)

function onTabChange(name: string | number) {
  const seg = String(name)
  const target = `/ops/${seg}`
  if (route.path !== target) router.push(target)
}
</script>

<style scoped>
.ops-center {
  padding: 16px;
}
</style>
