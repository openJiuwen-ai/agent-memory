<template>
  <div class="governance">
    <!-- 治理概览 + 保留清理 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div class="card-header">
          <span>治理概览</span>
          <div class="filter-bar">
            <el-input v-model="scopeId" placeholder="Scope ID" size="small" clearable style="width: 180px" />
            <el-input v-model="userId" placeholder="用户 ID" size="small" clearable style="width: 140px" />
            <el-button size="small" :icon="Refresh" @click="loadBundle">刷新</el-button>
            <el-button size="small" type="warning" @click="onCleanup(true)" :disabled="!userStore.hasPermission('governance:read')">预览过期</el-button>
            <el-button size="small" type="danger" @click="onCleanup(false)" :disabled="!userStore.hasPermission('governance:write')">执行清理</el-button>
          </div>
        </div>
      </template>
      <el-descriptions v-loading="bundleLoading" :column="3" border size="small">
        <el-descriptions-item label="记忆总数">{{ bundle?.governance_summary?.total ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="过期数">{{ bundle?.governance_summary?.expired_count ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="配额使用%">{{ bundle?.governance_summary?.quota_usage_percent ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="TTL(天)">{{ bundle?.retention?.ttl_days ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="配额上限">{{ bundle?.quota_status?.limit ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="当前用量">{{ bundle?.quota_status?.usage ?? '-' }}</el-descriptions-item>
      </el-descriptions>
      <div v-if="cleanupResult" class="cleanup-result">
        <el-divider content-position="left">{{ cleanupResult.dry_run ? '清理预览（dry_run）' : '清理执行结果' }}</el-divider>
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="TTL(天)">{{ cleanupResult.ttl_days }}</el-descriptions-item>
          <el-descriptions-item label="过期数">{{ cleanupResult.expired_count }}</el-descriptions-item>
          <el-descriptions-item label="已删除">{{ cleanupResult.deleted_count ?? 0 }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="(cleanupResult.items || []).length" class="hint">涉及记忆ID（前若干）: {{ (cleanupResult.items || []).slice(0, 8).join(', ') }}{{ cleanupResult.items.length > 8 ? ' ...' : '' }}</div>
      </div>
    </el-card>

    <!-- 治理策略配置 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>治理策略</span>
          <el-button type="primary" :loading="saving" @click="handleSave" :disabled="!userStore.hasPermission('governance:write')">保存策略</el-button>
        </div>
      </template>

      <el-form v-loading="strategyLoading" :model="strategy" label-width="160px">
        <el-divider content-position="left">保留策略（生命周期）</el-divider>
        <el-form-item label="启用 TTL 清理">
          <el-switch v-model="strategy.lifecycle.enabled" active-text="启用" inactive-text="禁用" />
          <span v-if="strategy.lifecycle.enabled" style="margin-left: 16px">
            过期阈值: <el-input-number v-model="strategy.lifecycle.ttlDays" :min="1" :max="3650" style="width: 120px" /> 天
          </span>
        </el-form-item>

        <el-divider content-position="left">配额</el-divider>
        <el-form-item label="启用配额">
          <el-switch v-model="strategy.quota.enabled" active-text="启用" inactive-text="禁用" />
        </el-form-item>
        <div v-if="strategy.quota.enabled" style="margin-left: 28px; margin-bottom: 16px">
          <el-form-item label="最大Scope数">
            <el-input-number v-model="strategy.quota.maxScopes" :min="1" :max="10000" style="width: 200px" />
          </el-form-item>
          <el-form-item label="每用户最大记忆数">
            <el-input-number v-model="strategy.quota.maxMemoriesPerUser" :min="1" :max="1000000" style="width: 200px" />
          </el-form-item>
          <el-form-item label="每日消息量">
            <el-input-number v-model="strategy.quota.maxDailyMessages" :min="1" :max="10000000" style="width: 200px" />
          </el-form-item>
        </div>
      </el-form>
      <div class="hint">
        说明：去重/合并/合规护栏属引擎职责（:8516 MemUpdateChecker + forbidden_variables），本页只管保留策略(TTL 清理) + 配额。
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  getGovernanceStrategy,
  saveGovernanceStrategy,
  getGovernancePage,
  runGovernanceCleanup,
} from '@/api/governance'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

// 本地联调默认值（记忆挂在 user=r5 / scope=demo 下）。
const scopeId = ref('demo')
const userId = ref('r5')

const strategyLoading = ref(false)
const saving = ref(false)
const bundleLoading = ref(false)
const bundle = ref<any>(null)
const cleanupResult = ref<any>(null)

const strategy = reactive({
  lifecycle: { enabled: true, ttlDays: 90 },
  quota: { enabled: true, maxScopes: 100, maxMemoriesPerUser: 100000, maxDailyMessages: 1000000 },
})

async function loadStrategy() {
  strategyLoading.value = true
  try {
    const s = await getGovernanceStrategy()
    if (s) {
      Object.assign(strategy.lifecycle, s.lifecycle ?? {})
      Object.assign(strategy.quota, s.quota ?? {})
    }
  } catch (e) {
    // 错误已由拦截器提示
  } finally {
    strategyLoading.value = false
  }
}

async function loadBundle() {
  bundleLoading.value = true
  cleanupResult.value = null
  try {
    bundle.value = await getGovernancePage(userId.value || undefined, scopeId.value || undefined)
  } catch (e) {
    bundle.value = null
  } finally {
    bundleLoading.value = false
  }
}

async function handleSave() {
  saving.value = true
  try {
    await saveGovernanceStrategy(JSON.parse(JSON.stringify(strategy)))
    ElMessage.success('治理策略保存成功')
    await loadBundle()
  } catch (e) {
    // 错误已由拦截器提示
  } finally {
    saving.value = false
  }
}

async function onCleanup(dryRun: boolean) {
  if (!dryRun) {
    try {
      await ElMessageBox.confirm('将按 TTL 真实删除过期记忆，不可恢复，确认执行？', '警告', { type: 'warning' })
    } catch {
      return
    }
  }
  try {
    cleanupResult.value = await runGovernanceCleanup(userId.value || undefined, scopeId.value || undefined, dryRun)
    ElMessage.success(
      dryRun
        ? `预览完成：过期 ${cleanupResult.value?.expired_count ?? 0} / 共 ${cleanupResult.value?.total ?? 0}`
        : `清理完成：删除 ${cleanupResult.value?.deleted_count ?? 0} / 过期 ${cleanupResult.value?.expired_count ?? 0}`,
    )
    await loadBundle()
  } catch (e) {
    // 错误已由拦截器提示
  }
}

onMounted(() => {
  loadStrategy()
  loadBundle()
})
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
.cleanup-result {
  margin-top: 8px;
}
.hint {
  margin-top: 12px;
  color: #909399;
  font-size: 12px;
}
</style>
