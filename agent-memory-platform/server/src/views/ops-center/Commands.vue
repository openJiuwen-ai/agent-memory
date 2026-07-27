<template>
  <div class="ops-commands">
    <!-- 命令目录 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>命令目录</template>
      <el-table v-loading="catalogLoading" :data="commands" border size="small">
        <el-table-column prop="command_code" label="命令编码" width="180" />
        <el-table-column prop="command_name" label="名称" min-width="160" />
        <el-table-column prop="category" label="分类" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ row.category }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="可用" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '是' : '否' }}</el-tag>
          </template>
        </el-table-column>
        <!-- <el-table-column prop="gap_reason" label="缺口/未接入原因" min-width="200" show-overflow-tooltip /> -->
        <el-table-column label="需确认" width="80">
          <template #default="{ row }">{{ row.require_confirm ? '是' : '否' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button v-if="userStore.hasPermission('ops:write')" type="primary" link size="small" @click="openDispatch(row)">下发</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 执行历史 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>执行历史</span>
          <div class="filter-bar">
            <el-input v-model="filterCode" placeholder="命令编码" clearable size="small" style="width: 180px" />
            <el-select v-model="filterStatus" placeholder="状态" clearable size="small" style="width: 120px">
              <el-option label="success" value="success" />
              <el-option label="gap" value="gap" />
              <el-option label="dry_run" value="dry_run" />
              <el-option label="failed" value="failed" />
            </el-select>
            <el-button size="small" :icon="Refresh" @click="loadExecutions">刷新</el-button>
          </div>
        </div>
      </template>
      <el-table v-loading="execLoading" :data="executions" border size="small" @row-click="openDetail">
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column prop="command_code" label="命令" width="180" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" label="耗时(ms)" width="100" />
        <el-table-column prop="reason" label="事由" min-width="140" show-overflow-tooltip />
        <el-table-column prop="gap_hint" label="缺口提示" min-width="200" show-overflow-tooltip />
      </el-table>
      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="page.page"
          v-model:page-size="page.size"
          :total="page.total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @size-change="onSizeChange"
          @current-change="loadExecutions"
        />
      </div>
    </el-card>

    <!-- 下发弹框 -->
    <el-dialog v-model="dispatchVisible" :title="`下发命令: ${current?.command_code ?? ''}`" width="560px">
      <el-descriptions :column="1" border size="small" style="margin-bottom: 12px">
        <el-descriptions-item label="名称">{{ current?.command_name }}</el-descriptions-item>
        <el-descriptions-item label="通道">{{ current?.backend_action }}</el-descriptions-item>
        <el-descriptions-item label="说明">{{ current?.description || '-' }}</el-descriptions-item>
      </el-descriptions>
      <el-form :model="dispatchForm" label-width="100px">
        <el-form-item label="Scope ID">
          <el-input v-model="dispatchForm.scopeId" placeholder="可选" clearable />
        </el-form-item>
        <el-form-item label="用户 ID">
          <el-input v-model="dispatchForm.userId" placeholder="可选" clearable />
        </el-form-item>
        <el-form-item label="事由">
          <el-input v-model="dispatchForm.reason" placeholder="运维事由" />
        </el-form-item>
        <el-form-item label="DryRun 预演">
          <el-switch v-model="dispatchForm.dryRun" active-text="预演(不实际下发)" inactive-text="实际下发" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dispatchVisible = false">取消</el-button>
        <el-button type="primary" :loading="dispatching" @click="doDispatch">确认下发</el-button>
      </template>
    </el-dialog>

    <!-- 执行详情 -->
    <el-dialog v-model="detailVisible" title="执行详情" width="640px">
      <el-descriptions v-if="detail" :column="1" border size="small">
        <el-descriptions-item label="执行ID">{{ detail.execution_id }}</el-descriptions-item>
        <el-descriptions-item label="命令">{{ detail.command_code }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ detail.status }}</el-descriptions-item>
        <el-descriptions-item label="耗时">{{ detail.duration_ms }} ms</el-descriptions-item>
        <el-descriptions-item label="操作人">{{ detail.operator_id || '-' }}</el-descriptions-item>
        <el-descriptions-item label="事由">{{ detail.reason || '-' }}</el-descriptions-item>
        <el-descriptions-item label="缺口提示">{{ detail.gap_hint || '-' }}</el-descriptions-item>
        <el-descriptions-item label="入参快照">
          <pre class="snapshot">{{ pretty(detail.payload_snapshot) }}</pre>
        </el-descriptions-item>
        <el-descriptions-item label="结果快照">
          <pre class="snapshot">{{ pretty(detail.result_snapshot) }}</pre>
        </el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { listCommands, dispatchCommand, listExecutions, getExecution } from '@/api/ops-commands'
import { issueKernelConfirmToken } from '@/api/config'
import type { OpsCommand, CommandExecution } from '@/types/ops-commands'

const userStore = useUserStore()

const catalogLoading = ref(false)
const execLoading = ref(false)
const commands = ref<OpsCommand[]>([])
const executions = ref<CommandExecution[]>([])
const page = reactive({ page: 1, size: 20, total: 0 })
const filterCode = ref('')
const filterStatus = ref('')

const dispatchVisible = ref(false)
const dispatching = ref(false)
const current = ref<OpsCommand | null>(null)
const dispatchForm = reactive({ scopeId: '', userId: '', reason: '', dryRun: false })

const detailVisible = ref(false)
const detail = ref<CommandExecution | null>(null)

function statusType(s: string) {
  return ({ success: 'success', gap: 'warning', dry_run: 'info', failed: 'danger' } as Record<string, any>)[s] ?? 'info'
}

async function loadCommands() {
  catalogLoading.value = true
  try {
    commands.value = await listCommands()
  } catch (e) {
    commands.value = []
  } finally {
    catalogLoading.value = false
  }
}

async function loadExecutions() {
  execLoading.value = true
  try {
    const res = await listExecutions({
      page_idx: page.page,
      page_size: page.size,
      command_code: filterCode.value || undefined,
      status: filterStatus.value || undefined,
    })
    executions.value = res.items || []
    page.total = res.total ?? 0
  } catch (e) {
    executions.value = []
  } finally {
    execLoading.value = false
  }
}

function onSizeChange() {
  page.page = 1
  loadExecutions()
}

function openDispatch(row: OpsCommand) {
  current.value = row
  dispatchForm.scopeId = ''
  dispatchForm.userId = ''
  dispatchForm.reason = ''
  dispatchForm.dryRun = false
  dispatchVisible.value = true
}

async function doDispatch() {
  if (!current.value) return
  dispatching.value = true
  try {
    // 高危命令需二次确认令牌（后端 dispatch 检查 requireConfirm/HIGH_RISK_COMMANDS）
    let payload: Record<string, any> | undefined
    if (current.value.require_confirm) {
      try {
        const { confirmToken } = await issueKernelConfirmToken()
        payload = { confirmToken }
      } catch (e: any) {
        ElMessage.error('高危命令需要确认令牌，获取失败: ' + (e?.message || ''))
        dispatching.value = false
        return
      }
    }
    const res = await dispatchCommand({
      commandCode: current.value.command_code,
      scopeId: dispatchForm.scopeId || undefined,
      userId: dispatchForm.userId || undefined,
      reason: dispatchForm.reason || undefined,
      dryRun: dispatchForm.dryRun,
      payload,
    })
    const msg = `${res.status}${res.gap_hint ? ' / ' + res.gap_hint : ''}（${res.duration_ms}ms）`
    if (res.status === 'success' || res.status === 'dry_run') {
      ElMessage.success(msg)
    } else {
      ElMessage.warning(msg)
    }
    dispatchVisible.value = false
    await loadExecutions()
  } catch (e) {
    // 错误已由拦截器提示
  } finally {
    dispatching.value = false
  }
}

async function openDetail(row: CommandExecution) {
  try {
    detail.value = await getExecution(row.execution_id)
    detailVisible.value = true
  } catch (e) {
    // 错误已由拦截器提示
  }
}

function pretty(s?: string | null): string {
  if (!s) return '-'
  try {
    return JSON.stringify(JSON.parse(s), null, 2)
  } catch {
    return s
  }
}

onMounted(() => {
  loadCommands()
  loadExecutions()
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
.pagination-bar {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
.snapshot {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow: auto;
  font-size: 12px;
}
</style>
