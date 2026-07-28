<template>
  <div class="ops-tasks">
    <!-- 任务列表 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div class="card-header">
          <span>任务列表</span>
          <div class="filter-bar">
            <el-select v-model="filterType" placeholder="任务类型" clearable size="small" style="width: 140px" @change="loadTasks">
              <el-option label="Dreaming" value="DREAMING" />
              <el-option label="迁移" value="MIGRATION" />
            </el-select>
            <el-select v-model="filterStatus" placeholder="状态" clearable size="small" style="width: 120px" @change="loadTasks">
              <el-option label="pending" value="pending" />
              <el-option label="running" value="running" />
              <el-option label="stopped" value="stopped" />
              <el-option label="failed" value="failed" />
              <el-option label="completed" value="completed" />
            </el-select>
            <el-button type="primary" size="small" :icon="Refresh" @click="loadTasks">刷新</el-button>
          </div>
        </div>
      </template>
      <el-table v-loading="tasksLoading" :data="pagedTasks" border size="small" height="400">
        <el-table-column prop="task_type" label="类型" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.task_type === 'DREAMING' ? 'success' : 'warning'">{{ row.task_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="scope_id" label="Scope" width="140" show-overflow-tooltip />
        <el-table-column prop="user_id" label="用户" width="110" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="started_at" label="启动时间" width="170" />
        <el-table-column prop="stopped_at" label="停止时间" width="170" />
        <el-table-column prop="last_heartbeat" label="最近心跳" width="170" />
        <el-table-column prop="error_message" label="错误" min-width="160" show-overflow-tooltip />
      </el-table>
      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="pagination.page"
          v-model:page-size="pagination.size"
          :total="tasks.length"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @size-change="() => pagination.page = 1"
        />
      </div>
    </el-card>

    <!-- Dreaming 启停 + 结果 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div class="card-header">
          <span>Dreaming（跨会话睡时巩固）</span>
          <div class="action-buttons">
            <el-select v-model="dreamingFilter.scopeId" placeholder="Scope" clearable filterable allow-create default-first-option size="small" style="width: 160px">
              <el-option v-for="s in scopeOptions" :key="s.scopeId" :label="s.scopeName || s.scopeId" :value="s.scopeId" />
            </el-select>
            <el-input v-model="dreamingFilter.userId" placeholder="用户 ID" size="small" style="width: 100px" />
            <el-button type="primary" size="small" :loading="starting" @click="onStart">启动</el-button>
            <el-button type="danger" size="small" :loading="stopping" @click="onStop">停止</el-button>
            <el-button size="small" :icon="Refresh" @click="loadDreaming">刷新</el-button>
          </div>
        </div>
      </template>

      <el-descriptions v-if="dreamingResult" :column="3" border size="small">
        <el-descriptions-item label="运行状态">
          <el-tag size="small" :type="dreamingResult.running ? 'success' : 'info'">
            {{ dreamingResult.running ? '运行中' : '已停止' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Sweep 间隔">{{ dreamingResult.interval_seconds }} s</el-descriptions-item>
        <el-descriptions-item label="下次预计">{{ dreamingResult.next_estimated_ts || '-' }}</el-descriptions-item>
        <el-descriptions-item label="最近扫描时间">{{ dreamingResult.last_scan_ts || '-' }}</el-descriptions-item>
        <el-descriptions-item label="累计已扫会话">{{ dreamingResult.scanned_sessions_count }}</el-descriptions-item>
        <el-descriptions-item label="最近产出条数">{{ dreamingResult.last_promoted_count ?? '-' }}</el-descriptions-item>
      </el-descriptions>
      <el-alert
        v-if="dreamingResult?.error"
        type="error"
        :closable="false"
        :title="dreamingResult.error"
        show-icon
        style="margin-top: 12px"
      />
      <el-alert
        v-if="!dreamingResult"
        type="info"
        :closable="false"
        title="Dreaming 未运行"
        description="填入 Scope ID / 用户 ID 后点「启动」开始跨会话巩固；运行中点「刷新」查看最近扫描时间/累计已扫会话等。"
        show-icon
      />
    </el-card>

    <!-- 迁移区 -->
    <el-card shadow="never">
      <template #header>数据迁移</template>
      <el-table v-loading="migrationsLoading" :data="migrations" border size="small">
        <el-table-column prop="task_type" label="类型" width="100">
          <template #default><el-tag size="small" type="warning">MIGRATION</el-tag></template>
        </el-table-column>
        <el-table-column prop="scope_id" label="Scope" width="160" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="started_at" label="启动时间" width="170" />
        <el-table-column prop="stopped_at" label="完成时间" width="170" />
        <el-table-column label="操作" width="120">
          <template #default>
            <el-button type="primary" link size="small" @click="goMigration">迁移详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  listTasks,
  startDreaming,
  stopDreaming,
  getDreamingResult,
} from '@/api/ops-tasks'
import { getAllScopes } from '@/api/scope'
import type { TaskRecord, DreamingResult, TaskStatus } from '@/types/ops-tasks'
import type { ScopeRegistry } from '@/types/scope'

const tasks = ref<TaskRecord[]>([])
const migrations = ref<TaskRecord[]>([])
const tasksLoading = ref(false)
const migrationsLoading = ref(false)
const scopeOptions = ref<ScopeRegistry[]>([])

const pagination = reactive({ page: 1, size: 10 })
const pagedTasks = computed(() => {
  const start = (pagination.page - 1) * pagination.size
  return tasks.value.slice(start, start + pagination.size)
})

const filterType = ref<TaskRecord['task_type'] | ''>('')
const filterStatus = ref<TaskStatus | ''>('')

const dreamingResult = ref<DreamingResult | null>(null)
const starting = ref(false)
const stopping = ref(false)

const dreamingFilter = reactive({ scopeId: '', userId: '' })

function statusType(s: string) {
  return { running: 'success', completed: 'info', failed: 'danger', stopped: 'warning', pending: 'info' }[s] ?? 'info'
}

async function loadTasks() {
  tasksLoading.value = true
  try {
    const res = await listTasks({
      task_type: filterType.value || undefined,
      status: filterStatus.value || undefined,
    } as any)
    tasks.value = res.items
  } finally {
    tasksLoading.value = false
  }
}

async function loadMigrations() {
  migrationsLoading.value = true
  try {
    const res = await listTasks({ task_type: 'MIGRATION' })
    migrations.value = res.items
  } finally {
    migrationsLoading.value = false
  }
}

async function loadDreaming() {
  try {
    dreamingResult.value = await getDreamingResult(dreamingFilter.scopeId, dreamingFilter.userId)
  } catch (e: any) {
    dreamingResult.value = null
  }
}

async function onStart() {
  starting.value = true
  try {
    await startDreaming(dreamingFilter.scopeId, dreamingFilter.userId)
    ElMessage.success('Dreaming 已启动')
    await loadDreaming()
    await loadTasks()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '启动失败')
  } finally {
    starting.value = false
  }
}

async function onStop() {
  stopping.value = true
  try {
    await stopDreaming(dreamingFilter.scopeId, dreamingFilter.userId)
    ElMessage.success('Dreaming 已停止')
    await loadDreaming()
    await loadTasks()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '停止失败')
  } finally {
    stopping.value = false
  }
}

function goMigration() {
  ElMessage.info('迁移详情归 F9，待接入')
}

onMounted(async () => {
  try {
    scopeOptions.value = await getAllScopes()
    if (scopeOptions.value.length > 0) dreamingFilter.scopeId = scopeOptions.value[0].scopeId
  } catch { /* request 拦截器已提示 */ }
  loadTasks()
  loadMigrations()
  loadDreaming()
})
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.filter-bar,
.action-buttons {
  display: flex;
  gap: 8px;
  align-items: center;
}

.hint {
  margin-top: 12px;
  color: #909399;
  font-size: 12px;
}
.pagination-bar {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
