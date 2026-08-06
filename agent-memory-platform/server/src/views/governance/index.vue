<template>
  <div class="governance-page">
    <!-- 治理概览 -->
    <el-row :gutter="20" class="overview-row">
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-card">
            <div class="stat-label">活跃清理任务</div>
            <div class="stat-value">{{ governance?.active_cleanup_tasks ?? 0 }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-card">
            <div class="stat-label">扫描问题</div>
            <div class="stat-value">{{ governance?.last_scan_issues ?? 0 }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-card">
            <div class="stat-label">合规违规</div>
            <div class="stat-value">{{ governance?.compliance_violations ?? 0 }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-card">
            <div class="stat-label">配额使用</div>
            <div class="stat-value">{{ governance?.quota_usage_percent ?? 0 }}%</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 扫描结果 -->
    <el-card shadow="never" class="section-card">
      <template #header>扫描结果</template>
      <el-descriptions :column="3" border>
        <el-descriptions-item label="重复记忆">{{ scanResults?.duplicate_count ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="过期记忆">{{ scanResults?.stale_count ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="空记忆">{{ scanResults?.empty_count ?? 0 }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 合规状态 -->
    <el-card shadow="never" class="section-card">
      <template #header>合规状态</template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="禁用变量">
          <el-tag v-for="v in compliance?.forbidden_variables" :key="v" style="margin: 2px">{{ v }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="违规数量">{{ compliance?.violation_count ?? 0 }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 配额状态 -->
    <el-card shadow="never" class="section-card">
      <template #header>配额状态</template>
      <el-progress :percentage="quota?.usage_percent ?? 0" :status="quotaStatus" />
      <div class="quota-detail">
        <span>已用: {{ quota?.usage ?? 0 }}</span>
        <span>上限: {{ quota?.limit ?? 100000 }}</span>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getGovernancePage } from '@/api/governance'

const governance = ref<any>(null)
const scanResults = ref<any>(null)
const compliance = ref<any>(null)
const quota = ref<any>(null)

const quotaStatus = computed(() => {
  const percent = quota.value?.usage_percent ?? 0
  if (percent >= 90) return 'exception'
  if (percent >= 70) return 'warning'
  return 'success'
})

onMounted(async () => {
  const res = await getGovernancePage()
  governance.value = res.governance_summary
  scanResults.value = res.scan_results
  compliance.value = res.compliance_status
  quota.value = res.quota_status
})
</script>

<style scoped>
.overview-row { margin-bottom: 20px; }
.stat-card { text-align: center; padding: 16px 0; }
.stat-label { font-size: 13px; color: #909399; }
.stat-value { font-size: 28px; font-weight: 600; color: #409eff; margin-top: 8px; }
.section-card { margin-bottom: 16px; }
.quota-detail { display: flex; justify-content: space-between; margin-top: 8px; font-size: 13px; color: #606266; }
</style>
