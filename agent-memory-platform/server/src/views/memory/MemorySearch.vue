<template>
  <div class="memory-search">
    <div class="search-bar">
      <el-select v-model="scopeId" placeholder="Scope" clearable filterable allow-create default-first-option style="width: 200px">
        <el-option v-for="s in scopeOptions" :key="s.scopeId" :label="s.scopeName || s.scopeId" :value="s.scopeId" />
      </el-select>
      <el-input v-model="userId" placeholder="用户 ID" clearable style="width: 140px" />
      <el-input v-model="queryStr" placeholder="输入关键字搜索记忆" style="width: 360px" @keyup.enter="handleSearch">
        <template #append><el-button :icon="Search" @click="handleSearch">搜索</el-button></template>
      </el-input>
    </div>
    <el-table v-if="results.length" :data="results" border v-loading="loading">
      <el-table-column prop="mem_id" label="记忆ID" width="220" />
      <el-table-column prop="memory_type" label="类型" width="120" />
      <el-table-column prop="content" label="内容" min-width="200" show-overflow-tooltip />
      <el-table-column prop="score" label="相似度" width="100">
        <template #default="{ row }">{{ ((row.score ?? 0) * 100).toFixed(1) }}%</template>
      </el-table-column>
    </el-table>
    <el-empty v-else description="输入关键字开始搜索" />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { searchMemories } from '@/api/memory'
import { getAllScopes } from '@/api/scope'
import type { ScopeRegistry } from '@/types/scope'

const scopeId = ref('')
const userId = ref('')
const scopeOptions = ref<ScopeRegistry[]>([])
const queryStr = ref('')
const results = ref<any[]>([])
const loading = ref(false)

onMounted(async () => {
  try {
    scopeOptions.value = await getAllScopes()
    if (scopeOptions.value.length > 0) scopeId.value = scopeOptions.value[0].scopeId
  } catch { /* request 拦截器已提示 */ }
})

async function handleSearch() {
  if (!queryStr.value.trim()) return
  loading.value = true
  try {
    results.value = await searchMemories({
      query: queryStr.value,
      num: 20,
      threshold: 0.3,
      user_id: userId.value || undefined,
      scope_id: scopeId.value || undefined,
    })
  } catch (e) {
    // 错误已由 request 拦截器提示
    results.value = []
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.search-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
</style>
