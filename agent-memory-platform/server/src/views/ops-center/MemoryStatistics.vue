<template>
  <div class="memory-statistics">
    <!-- 统计卡片 -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-label">总记忆数</div>
          <div class="stat-value">{{ stats.totalMemories.toLocaleString() }}</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-label">本周写入</div>
          <div class="stat-value">{{ stats.weeklyWrites }}</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-label">本周修改</div>
          <div class="stat-value">{{ stats.weeklyUpdates }}</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-label">本周删除</div>
          <div class="stat-value">{{ stats.weeklyDeletes }}</div>
        </div>
      </el-col>
    </el-row>

    <!-- 按类型分布 -->
    <el-card shadow="never" class="section-card">
      <template #header>
        <span>按类型分布</span>
      </template>
      <div class="distribution-list">
        <div v-for="item in typeDistribution" :key="item.type" class="distribution-item">
          <div class="distribution-label">{{ item.label }}</div>
          <div class="distribution-bar">
            <div class="bar-fill" :style="{ width: item.percent + '%' }"></div>
          </div>
          <div class="distribution-value">{{ item.count.toLocaleString() }} ({{ item.percent }}%)</div>
        </div>
      </div>
    </el-card>

    <!-- 按Scope分布 -->
    <el-card shadow="never" class="section-card">
      <template #header>
        <span>按Scope分布</span>
      </template>
      <div class="distribution-list">
        <div v-for="item in scopeDistribution" :key="item.scope" class="distribution-item">
          <div class="distribution-label">{{ item.label }}</div>
          <div class="distribution-bar">
            <div class="bar-fill" :style="{ width: (item.count / maxScopeCount * 100) + '%' }"></div>
          </div>
          <div class="distribution-value">{{ item.count.toLocaleString() }}</div>
        </div>
      </div>
    </el-card>

    <!-- 增长趋势 -->
    <el-card shadow="never" class="section-card">
      <template #header>
        <span>增长趋势 (最近7天)</span>
      </template>
      <el-table :data="growthTrend" border>
        <el-table-column prop="date" label="日期" width="120" />
        <el-table-column prop="writes" label="写入" width="100" />
        <el-table-column prop="updates" label="修改" width="100" />
        <el-table-column prop="deletes" label="删除" width="100" />
        <el-table-column label="净增长" width="120">
          <template #default="{ row }">
            <span :class="row.netGrowth >= 0 ? 'positive' : 'negative'">
              {{ row.netGrowth >= 0 ? '+' : '' }}{{ row.netGrowth }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 存储用量 -->
    <el-card shadow="never" class="section-card">
      <template #header>
        <span>存储用量</span>
      </template>
      <div class="storage-usage">
        <div class="storage-info">
          已用: {{ storageUsage.used }} MB / {{ storageUsage.total }} MB ({{ storageUsage.percent }}%)
        </div>
        <el-progress :percentage="storageUsage.percent" :color="progressColor" />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'

const stats = ref({
  totalMemories: 12345,
  weeklyWrites: 456,
  weeklyUpdates: 89,
  weeklyDeletes: 23,
})

const typeDistribution = ref([
  { type: 'user_profile', label: '用户画像', count: 4523, percent: 36.7 },
  { type: 'semantic_memory', label: '语义记忆', count: 3891, percent: 31.5 },
  { type: 'episodic_memory', label: '情景记忆', count: 2145, percent: 17.4 },
  { type: 'summary', label: '摘要', count: 1234, percent: 10.0 },
  { type: 'variable', label: '变量', count: 552, percent: 4.4 },
])

const scopeDistribution = ref([
  { scope: 'project-A', label: 'project-A', count: 5678 },
  { scope: 'project-B', label: 'project-B', count: 3456 },
  { scope: 'agent-X', label: 'agent-X', count: 2123 },
  { scope: 'session-Y', label: 'session-Y', count: 1088 },
])

const growthTrend = ref([
  { date: '07-03', writes: 78, updates: 12, deletes: 3, netGrowth: 63 },
  { date: '07-04', writes: 65, updates: 8, deletes: 1, netGrowth: 56 },
  { date: '07-05', writes: 92, updates: 15, deletes: 5, netGrowth: 72 },
  { date: '07-06', writes: 45, updates: 23, deletes: 2, netGrowth: 20 },
  { date: '07-07', writes: 56, updates: 6, deletes: 8, netGrowth: 42 },
  { date: '07-08', writes: 67, updates: 18, deletes: 3, netGrowth: 46 },
  { date: '07-09', writes: 53, updates: 7, deletes: 1, netGrowth: 45 },
])

const storageUsage = ref({
  used: 4523,
  total: 10240,
  percent: 44.2,
})

const maxScopeCount = computed(() => {
  return Math.max(...scopeDistribution.value.map(item => item.count))
})

const progressColor = computed(() => {
  if (storageUsage.value.percent < 60) return '#67c23a'
  if (storageUsage.value.percent < 80) return '#e6a23c'
  return '#f56c6c'
})

onMounted(() => {
  // TODO: 调用后端API获取统计数据
})
</script>

<style scoped>
.memory-statistics {
  padding: 16px;
}

.stats-row {
  margin-bottom: 24px;
}

.stat-card {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 8px;
  padding: 24px;
  text-align: center;
  color: white;
}

.stat-label {
  font-size: 14px;
  opacity: 0.9;
  margin-bottom: 8px;
}

.stat-value {
  font-size: 32px;
  font-weight: 700;
}

.section-card {
  margin-bottom: 24px;
}

.distribution-list {
  padding: 8px 0;
}

.distribution-item {
  display: flex;
  align-items: center;
  margin-bottom: 16px;
  gap: 16px;
}

.distribution-label {
  width: 100px;
  font-weight: 600;
}

.distribution-bar {
  flex: 1;
  height: 24px;
  background: #f5f7fa;
  border-radius: 12px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  background: linear-gradient(90deg, #409eff 0%, #67c23a 100%);
  border-radius: 12px;
  transition: width 0.3s ease;
}

.distribution-value {
  width: 180px;
  text-align: right;
  font-size: 14px;
  color: #606266;
}

.positive {
  color: #67c23a;
  font-weight: 600;
}

.negative {
  color: #f56c6c;
  font-weight: 600;
}

.storage-usage {
  padding: 16px 0;
}

.storage-info {
  margin-bottom: 16px;
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}
</style>
