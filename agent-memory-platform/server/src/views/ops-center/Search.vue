<template>
  <div class="memory-search">
    <el-card shadow="never">
      <template #header>记忆检索</template>
      <div class="filter-bar">
        <el-input v-model="filter.query" placeholder="输入检索词..." clearable style="width: 300px" @keyup.enter="doSearch" />
        <el-select v-model="filter.scopeId" placeholder="Scope" clearable filterable allow-create default-first-option style="width: 200px">
          <el-option v-for="s in scopeOptions" :key="s.scopeId" :label="s.scopeName || s.scopeId" :value="s.scopeId" />
        </el-select>
        <el-input v-model="filter.userId" placeholder="用户 ID" clearable style="width: 140px" />
        <div class="num-input-group">
          <span class="num-label">TopK</span>
          <el-input-number v-model="filter.num" :min="1" :max="100" :step="1" size="small" controls-position="right" style="width: 90px" />
        </div>
        <div class="num-input-group">
          <span class="num-label">阈值</span>
          <el-input-number v-model="filter.threshold" :min="0" :max="1" :step="0.05" :precision="2" size="small" controls-position="right" style="width: 100px" />
        </div>
        <el-button type="primary" @click="doSearch" :loading="loading">检索</el-button>
      </div>

      <el-table :data="results" border v-loading="loading" style="margin-top: 16px">
        <el-table-column prop="mem_id" label="记忆ID" width="200" show-overflow-tooltip />
        <el-table-column prop="content" label="内容" min-width="300" show-overflow-tooltip />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="typeTag(row.memory_type)">{{ typeText(row.memory_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="score" label="相关度" width="100">
          <template #default="{ row }">
            {{ row.score != null ? row.score.toFixed(4) : '-' }}
          </template>
        </el-table-column>
      </el-table>

      <div class="empty-hint" v-if="!loading && results.length === 0 && searched">
        无匹配记忆。尝试降低 threshold 或换检索词。
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { searchMemories } from '@/api/memory'
import { getAllScopes } from '@/api/scope'
import type { MemorySearchResult, MemoryType } from '@/types/memory'
import type { ScopeRegistry } from '@/types/scope'

const loading = ref(false)
const searched = ref(false)
const results = ref<MemorySearchResult[]>([])
const scopeOptions = ref<ScopeRegistry[]>([])

const filter = reactive({
  query: '',
  scopeId: '',
  userId: '',
  num: 10,
  threshold: 0.7,
})

onMounted(async () => {
  try {
    scopeOptions.value = await getAllScopes()
    if (scopeOptions.value.length > 0) filter.scopeId = scopeOptions.value[0].scopeId
  } catch { /* request 拦截器已提示 */ }
})

function typeText(t: MemoryType): string {
  const map: Record<string, string> = {
    user_profile: '用户画像',
    semantic_memory: '语义记忆',
    episodic_memory: '情景记忆',
    summary: '摘要',
    variable: '变量',
  }
  return map[t] || t
}

function typeTag(t: string): string {
  const map: Record<string, string> = {
    user_profile: 'primary',
    semantic_memory: 'success',
    episodic_memory: 'warning',
    summary: 'info',
    variable: 'info',
  }
  return map[t] || 'info'
}

async function doSearch() {
  if (!filter.query.trim()) return
  loading.value = true
  searched.value = true
  try {
    results.value = await searchMemories({
      query: filter.query,
      scope_id: filter.scopeId || undefined,
      user_id: filter.userId || undefined,
      num: filter.num,
      threshold: filter.threshold,
    })
  } catch {
    results.value = []
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.num-input-group {
  display: flex;
  align-items: center;
  gap: 6px;
}
.num-label {
  font-size: 13px;
  color: #606266;
  white-space: nowrap;
}
.empty-hint {
  margin-top: 16px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}
.hint {
  margin-top: 12px;
  color: #909399;
  font-size: 12px;
}
</style>
