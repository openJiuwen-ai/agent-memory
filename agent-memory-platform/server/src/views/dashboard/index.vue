<template>
  <div class="dashboard">
    <!-- 欢迎区 -->
    <el-card shadow="never" class="welcome-card">
      <div class="welcome">
        <el-icon :size="48" color="#409eff"><Coin /></el-icon>
        <h2>欢迎使用记忆管理平台</h2>
        <p>当前登录用户：{{ userStore.username }}</p>
        <p>系统状态：<el-tag :type="dashboard?.status === 'healthy' ? 'success' : 'danger'">{{ dashboard?.status }}</el-tag></p>
      </div>
    </el-card>

    <!-- 记忆总量 -->
    <el-card shadow="never" class="section-card">
      <template #header>记忆总量</template>
      <el-row :gutter="20">
        <el-col :span="6" v-for="item in memoryStats" :key="item.label">
          <div class="stat-card">
            <div class="stat-label">{{ item.label }}</div>
            <div class="stat-value">{{ item.value }}</div>
          </div>
        </el-col>
      </el-row>
      <div ref="memoryPieRef" class="chart-container"></div>
    </el-card>

    <!-- 增长趋势 -->
    <el-card shadow="never" class="section-card">
      <template #header>增长趋势</template>
      <div ref="growthRef" class="chart-container"></div>
    </el-card>

    <!-- 分类分布 -->
    <el-row :gutter="20">
      <el-col :span="12">
        <el-card shadow="never" class="section-card">
          <template #header>按类型分布</template>
          <div ref="typeBarRef" class="chart-container"></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never" class="section-card">
          <template #header>按质量分布</template>
          <div ref="qualityPieRef" class="chart-container"></div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 检索分析 -->
    <el-card shadow="never" class="section-card">
      <template #header>检索分析</template>
      <el-row :gutter="20">
        <el-col :span="8">
          <div class="stat-card">
            <div class="stat-label">今日检索次数</div>
            <div class="stat-value">{{ retrieval?.today_count ?? 0 }}</div>
          </div>
        </el-col>
        <el-col :span="8">
          <div class="stat-card">
            <div class="stat-label">命中率</div>
            <div class="stat-value">{{ ((retrieval?.hit_rate ?? 0) * 100).toFixed(1) }}%</div>
          </div>
        </el-col>
        <el-col :span="8">
          <div class="stat-card">
            <div class="stat-label">热门查询词</div>
            <div class="hot-words">
              <el-tag v-for="word in retrieval?.hot_words" :key="word" size="small" style="margin: 2px">{{ word }}</el-tag>
            </div>
          </div>
        </el-col>
      </el-row>
      <div ref="retrievalRef" class="chart-container"></div>
    </el-card>

    <!-- 存储用量 -->
    <el-card shadow="never" class="section-card">
      <template #header>存储用量</template>
      <el-progress :percentage="storage?.usage_percent ?? 0" :status="storageStatus" />
      <div class="storage-detail">
        <span>已用: {{ storage?.usage_mb ?? 0 }} MB</span>
        <span>配额: {{ storage?.quota_mb ?? 1024 }} MB</span>
      </div>
      <div ref="storageRef" class="chart-container"></div>
    </el-card>

    <!-- 任务监控 -->
    <el-card shadow="never" class="section-card">
      <template #header>任务监控</template>
      <el-row :gutter="20">
        <el-col :span="8">
          <div class="stat-card">
            <div class="stat-label">队列长度</div>
            <div class="stat-value">{{ tasks?.queue_length ?? 0 }}</div>
          </div>
        </el-col>
        <el-col :span="8">
          <div class="stat-card">
            <div class="stat-label">平均处理时长</div>
            <div class="stat-value">{{ tasks?.avg_duration ?? 0 }}s</div>
          </div>
        </el-col>
        <el-col :span="8">
          <div class="stat-card">
            <div class="stat-label">成功率</div>
            <div class="stat-value">{{ ((tasks?.success_rate ?? 0) * 100).toFixed(1) }}%</div>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <!-- LLM 成本 -->
    <el-card shadow="never" class="section-card">
      <template #header>LLM 成本</template>
      <el-row :gutter="20">
        <el-col :span="12">
          <div class="stat-card">
            <div class="stat-label">Embedding 调用次数</div>
            <div class="stat-value">{{ llm?.embedding_calls ?? 0 }}</div>
          </div>
        </el-col>
        <el-col :span="12">
          <div class="stat-card">
            <div class="stat-label">LLM 调用次数</div>
            <div class="stat-value">{{ llm?.llm_calls ?? 0 }}</div>
          </div>
        </el-col>
      </el-row>
      <div ref="llmRef" class="chart-container"></div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { Coin } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { getDashboard } from '@/api/dashboard'
import * as echarts from 'echarts'

const userStore = useUserStore()
const dashboard = ref<any>(null)

// 图表引用
const memoryPieRef = ref<HTMLElement>()
const growthRef = ref<HTMLElement>()
const typeBarRef = ref<HTMLElement>()
const qualityPieRef = ref<HTMLElement>()
const retrievalRef = ref<HTMLElement>()
const storageRef = ref<HTMLElement>()
const llmRef = ref<HTMLElement>()

// 数据
const memoryStats = ref([
  { label: '总记忆数', value: 0 },
  { label: '活跃数', value: 0 },
  { label: '归档数', value: 0 },
  { label: '待清理数', value: 0 },
])
const retrieval = ref<any>(null)
const storage = ref<any>(null)
const tasks = ref<any>(null)
const llm = ref<any>(null)

const storageStatus = computed(() => {
  const percent = storage.value?.usage_percent ?? 0
  if (percent >= 90) return 'exception'
  if (percent >= 70) return 'warning'
  return 'success'
})

// 图表实例
let charts: echarts.ECharts[] = []

function initCharts() {
  // 记忆总量饼图
  if (memoryPieRef.value) {
    const chart = echarts.init(memoryPieRef.value)
    chart.setOption({
      tooltip: { trigger: 'item' },
      series: [{
        type: 'pie',
        radius: '50%',
        data: [
          { value: 42000, name: '活跃' },
          { value: 5000, name: '归档' },
          { value: 2000, name: '待清理' },
          { value: 1000, name: '回收站' },
        ],
      }],
    })
    charts.push(chart)
  }

  // 增长趋势折线图
  if (growthRef.value) {
    const chart = echarts.init(growthRef.value)
    chart.setOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: ['7/1', '7/2', '7/3', '7/4', '7/5', '7/6', '7/7', '7/8'] },
      yAxis: { type: 'value' },
      series: [{ data: [120, 132, 101, 134, 90, 230, 210, 150], type: 'line', smooth: true }],
    })
    charts.push(chart)
  }

  // 类型分布柱状图
  if (typeBarRef.value) {
    const chart = echarts.init(typeBarRef.value)
    chart.setOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: ['用户画像', '语义记忆', '情景记忆', '摘要', '变量'] },
      yAxis: { type: 'value' },
      series: [{ data: [5000, 15000, 12000, 8000, 10000], type: 'bar' }],
    })
    charts.push(chart)
  }

  // 质量分布饼图
  if (qualityPieRef.value) {
    const chart = echarts.init(qualityPieRef.value)
    chart.setOption({
      tooltip: { trigger: 'item' },
      series: [{
        type: 'pie',
        radius: '50%',
        data: [
          { value: 30000, name: 'A级' },
          { value: 12000, name: 'B级' },
          { value: 5000, name: 'C级' },
          { value: 3000, name: 'D级' },
        ],
      }],
    })
    charts.push(chart)
  }

  // 检索分析折线图
  if (retrievalRef.value) {
    const chart = echarts.init(retrievalRef.value)
    chart.setOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: ['7/1', '7/2', '7/3', '7/4', '7/5', '7/6', '7/7', '7/8'] },
      yAxis: { type: 'value' },
      series: [
        { name: '检索次数', data: [200, 220, 180, 250, 210, 300, 280, 320], type: 'line' },
        { name: '命中次数', data: [150, 170, 140, 190, 160, 230, 210, 250], type: 'line' },
      ],
    })
    charts.push(chart)
  }

  // 存储用量柱状图
  if (storageRef.value) {
    const chart = echarts.init(storageRef.value)
    chart.setOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: ['7/1', '7/2', '7/3', '7/4', '7/5', '7/6', '7/7', '7/8'] },
      yAxis: { type: 'value', name: 'MB' },
      series: [{ data: [450, 460, 470, 480, 490, 500, 505, 512], type: 'bar' }],
    })
    charts.push(chart)
  }

  // LLM 成本折线图
  if (llmRef.value) {
    const chart = echarts.init(llmRef.value)
    chart.setOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: ['7/1', '7/2', '7/3', '7/4', '7/5', '7/6', '7/7', '7/8'] },
      yAxis: { type: 'value' },
      series: [
        { name: 'Embedding', data: [500, 520, 480, 550, 510, 600, 580, 620], type: 'line' },
        { name: 'LLM', data: [200, 220, 180, 250, 210, 300, 280, 320], type: 'line' },
      ],
    })
    charts.push(chart)
  }
}

function handleResize() {
  charts.forEach(chart => chart.resize())
}

onMounted(async () => {
  dashboard.value = await getDashboard()
  memoryStats.value[0].value = dashboard.value?.total_memories ?? 0
  memoryStats.value[1].value = dashboard.value?.active_memories ?? 0
  memoryStats.value[2].value = dashboard.value?.archived_memories ?? 0
  memoryStats.value[3].value = dashboard.value?.pending_cleanup ?? 0
  retrieval.value = dashboard.value?.retrieval
  storage.value = dashboard.value?.storage
  tasks.value = dashboard.value?.tasks
  llm.value = dashboard.value?.llm

  setTimeout(() => initCharts(), 100)
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  charts.forEach(chart => chart.dispose())
  window.removeEventListener('resize', handleResize)
})
</script>

<style scoped>
.dashboard { display: flex; flex-direction: column; gap: 20px; }
.welcome-card { margin-bottom: 0; }
.welcome { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 40px 0; }
.section-card { margin-bottom: 0; }
.stat-card { text-align: center; padding: 20px 0; }
.stat-label { font-size: 13px; color: #909399; }
.stat-value { font-size: 28px; font-weight: 600; color: #409eff; margin-top: 8px; }
.chart-container { height: 300px; margin-top: 16px; }
.storage-detail { display: flex; justify-content: space-between; margin-top: 8px; font-size: 13px; color: #606266; }
.hot-words { display: flex; flex-wrap: wrap; justify-content: center; }
</style>
