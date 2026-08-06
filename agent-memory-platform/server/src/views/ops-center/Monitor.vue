<template>
  <div class="ops-monitor">
    <el-card shadow="never" v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>系统监控</span>
          <div class="filter-bar">
            <el-input v-model="scopeId" placeholder="Scope ID" size="small" clearable style="width: 180px" />
            <el-input v-model="userId" placeholder="用户 ID" size="small" clearable style="width: 140px" />
            <el-button type="primary" size="small" :icon="Refresh" @click="loadAll">刷新</el-button>
          </div>
        </div>
      </template>

      <el-descriptions :column="2" border>
        <el-descriptions-item label="服务状态">
          <el-tag :type="health?.status === 'healthy' ? 'success' : 'danger'" size="small">
            {{ health?.status || '-' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="健康信息">{{ health?.message || '-' }}</el-descriptions-item>
        <el-descriptions-item label="记忆总数">
          {{ count?.count ?? '-' }}
          <span v-if="count?.approximate" class="hint">(近似)</span>
        </el-descriptions-item>
        <el-descriptions-item label="统计范围">
          scope={{ scopeId || '__default__' }} / user={{ userId || '__default__' }}
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="count?.hint" class="hint" style="margin-top: 8px">{{ count.hint }}</div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { getHealthProbe, getMemoryCount } from '@/api/ops-new'

// 本地联调默认值（记忆挂在 user=r5 / scope=demo 下）；可清空改任意 user/scope。
const scopeId = ref('demo')
const userId = ref('r5')
const health = ref<{ status: string; message: string } | null>(null)
const count = ref<{ count: number; approximate: boolean; hint?: string } | null>(null)
const loading = ref(false)

async function loadAll() {
  loading.value = true
  try {
    const [h, c] = await Promise.all([
      getHealthProbe(),
      getMemoryCount(userId.value || undefined, scopeId.value || undefined),
    ])
    health.value = h
    count.value = c
  } catch (e) {
    // 错误已由 request 拦截器提示
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.filter-bar {
  display: flex;
  gap: 8px;
  align-items: center;
}
.hint {
  color: #909399;
  font-size: 12px;
}
</style>
