<template>
  <div class="memory-list">
    <el-tabs v-model="activeTab" class="mem-tabs">

      <!-- ========== Tab 1: 记忆列表 ========== -->
      <el-tab-pane label="记忆列表" name="memory">
        <!-- 筛选条件 -->
        <div class="filter-bar">
          <el-select v-model="filter.scopeId" placeholder="Scope" clearable filterable allow-create default-first-option style="width: 200px" @change="onScopeChange">
            <el-option v-for="s in scopeOptions" :key="s.scopeId" :label="s.scopeName || s.scopeId" :value="s.scopeId" />
          </el-select>
          <el-input v-model="filter.userId" placeholder="用户 ID" clearable style="width: 160px" />
          <el-select v-model="filter.memoryType" placeholder="类型" clearable style="width: 140px">
            <el-option label="全部" value="" />
            <el-option label="用户画像" value="user_profile" />
            <el-option label="语义记忆" value="semantic_memory" />
            <el-option label="情景记忆" value="episodic_memory" />
            <el-option label="摘要" value="summary" />
          </el-select>
          <el-button type="primary" @click="fetchMemories">查询</el-button>
          <el-button type="danger" @click="handleBatchDelete" :disabled="!userStore.hasPermission('memory:delete') || selectedMemories.length === 0">批量删除 ({{ selectedMemories.length }})</el-button>
        </div>

        <!-- 记忆列表表格 -->
        <el-table :data="memories" border v-loading="loading" @selection-change="handleSelectionChange">
          <el-table-column type="selection" width="55" />
          <el-table-column prop="memId" label="记忆ID" width="120" />
          <el-table-column prop="content" label="内容(摘要)" min-width="200" show-overflow-tooltip />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">
              <el-tag :type="getMemoryTypeTag(row.type)" size="small">
                {{ getMemoryTypeLabel(row.type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="scopeId" label="Scope ID" width="120" />
          <el-table-column prop="userId" label="用户" width="80" />
          <el-table-column prop="timestamp" label="时间" width="160" />
          <el-table-column label="操作" width="280" fixed="right">
            <template #default="{ row }">
              <el-button type="primary" link @click="handleViewDetail(row)">详情</el-button>
              <el-button type="warning" link @click="handleEdit(row)" :disabled="!userStore.hasPermission('memory:write')">编辑</el-button>
              <el-button type="danger" link @click="handleDelete(row)" :disabled="!userStore.hasPermission('memory:delete')">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <!-- 分页 -->
        <div class="pagination-bar">
          <el-pagination
            v-model:current-page="pagination.page"
            v-model:page-size="pagination.size"
            :total="pagination.total"
            :page-sizes="[10, 20, 50, 100]"
            layout="total, sizes, prev, pager, next, jumper"
            @size-change="handleSizeChange"
            @current-change="handlePageChange"
          />
        </div>
      </el-tab-pane>

      <!-- ========== Tab 2: 变量 ========== -->
      <el-tab-pane label="变量" name="variable">
        <div class="filter-bar">
          <el-select v-model="varFilter.scopeId" placeholder="Scope" clearable filterable allow-create default-first-option style="width: 200px">
            <el-option v-for="s in scopeOptions" :key="s.scopeId" :label="s.scopeName || s.scopeId" :value="s.scopeId" />
          </el-select>
          <el-input v-model="varFilter.userId" placeholder="用户 ID" clearable style="width: 140px" />
          <el-button type="primary" @click="fetchVariables">查询</el-button>
          <el-button type="success" @click="handleAddVariable" :disabled="!userStore.hasPermission('memory:write')">新增变量</el-button>
          <el-tooltip content="变量为 key-value 形式的配置型记忆，用于存储用户偏好、环境参数等结构化信息。" placement="top">
            <el-icon class="hint-icon"><InfoFilled /></el-icon>
          </el-tooltip>
        </div>

        <el-table :data="variables" border v-loading="varLoading">
          <el-table-column prop="name" label="变量名" min-width="200" />
          <el-table-column prop="value" label="值" min-width="280" show-overflow-tooltip />
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button type="warning" link @click="handleEditVariable(row)" :disabled="!userStore.hasPermission('memory:write')">编辑</el-button>
              <el-button type="danger" link @click="handleDeleteVariable(row)" :disabled="!userStore.hasPermission('memory:delete')">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

    </el-tabs>

    <!-- 记忆详情侧边栏 -->
    <el-drawer v-model="detailVisible" title="记忆详情" size="600px" direction="rtl" class="memory-detail-drawer">
      <div v-if="currentMemory" class="memory-detail">
        <!-- 记忆内容 -->
        <el-divider content-position="left">记忆内容</el-divider>
        <div class="detail-section">
          <div class="detail-content-text">{{ currentMemory.content }}</div>
        </div>

        <!-- 记忆信息 -->
        <el-divider content-position="left">记忆信息</el-divider>
        <div class="detail-meta-list">
          <div class="detail-meta-row">
            <span class="detail-label">记忆ID</span>
            <span class="detail-value">{{ currentMemory.memId }}</span>
          </div>
          <div class="detail-meta-row">
            <span class="detail-label">类型</span>
            <el-tag :type="getMemoryTypeTag(currentMemory.type)" size="small" effect="light">{{ getMemoryTypeLabel(currentMemory.type) }}</el-tag>
          </div>
          <div class="detail-meta-row">
            <span class="detail-label">Scope</span>
            <span class="detail-value">{{ currentMemory.scopeId || '—' }}</span>
          </div>
          <div class="detail-meta-row">
            <span class="detail-label">用户</span>
            <span class="detail-value">{{ currentMemory.userId || '—' }}</span>
          </div>
          <div class="detail-meta-row">
            <span class="detail-label">创建时间</span>
            <span class="detail-value">{{ currentMemory.timestamp || '—' }}</span>
          </div>
        </div>

        <!-- 来源消息 -->
        <el-divider content-position="left">来源消息</el-divider>
        <div v-loading="traceLoading" class="detail-section">
          <template v-if="traceData?.source_messages?.length">
            <div v-for="(msg, i) in traceData.source_messages" :key="i" class="source-msg">
              <el-tag size="small" :type="msg.role === 'user' ? 'primary' : msg.role === 'dreaming' ? 'warning' : 'success'" style="margin-right: 6px">
                {{ msg.role === 'user' ? '用户' : msg.role === 'dreaming' ? 'Dreaming' : '助手' }}
              </el-tag>
              <span class="source-msg-content">{{ msg.content }}</span>
            </div>
          </template>
          <el-empty v-else-if="!traceLoading" description="无来源消息" :image-size="40" />
        </div>

        <!-- 变更历史 -->
        <el-divider content-position="left">变更历史</el-divider>
        <div class="detail-section">
          <template v-if="traceData?.change_history?.length">
            <el-timeline>
              <el-timeline-item v-for="(item, i) in traceData.change_history" :key="i" :timestamp="item.time || ''">
                <div class="change-item">
                  <el-tag size="small" :type="item.change_source === 'UPDATE' ? 'warning' : 'success'">{{ item.action || item.change_source }}</el-tag>
                  <span v-if="item.version" class="change-version">{{ item.version }}</span>
                  <div v-if="item.old_content" class="change-diff"><span class="change-old">修改前：{{ item.old_content }}</span></div>
                  <div v-if="item.content" class="change-diff"><span class="change-new">修改后：{{ item.content }}</span></div>
                  <span v-if="item.reason" class="change-reason">原因：{{ item.reason }}</span>
                </div>
              </el-timeline-item>
            </el-timeline>
          </template>
          <el-empty v-else description="无变更记录" :image-size="40" />
        </div>

        <!-- 操作审计 -->
        <el-divider content-position="left">操作审计</el-divider>
        <div class="detail-section">
          <template v-if="traceData?.audit_trail?.length">
            <el-table :data="traceData.audit_trail" size="small" border>
              <el-table-column prop="operation" label="操作" width="80" />
              <el-table-column prop="operator" label="操作人" width="100" show-overflow-tooltip />
              <el-table-column prop="operator_type" label="类型" width="80" />
              <el-table-column prop="time" label="时间" width="160" />
              <el-table-column prop="detail" label="详情" min-width="120" show-overflow-tooltip />
            </el-table>
          </template>
          <el-empty v-else description="无审计记录" :image-size="40" />
        </div>
      </div>

      <template #footer>
        <el-button v-if="userStore.hasPermission('memory:write')" type="warning" @click="handleEdit(currentMemory)">编辑</el-button>
        <el-button v-if="userStore.hasPermission('memory:delete')" type="danger" @click="handleDelete(currentMemory)">删除</el-button>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-drawer>

    <!-- 编辑记忆弹框 -->
    <el-dialog v-model="editVisible" title="编辑记忆" width="600px">
      <el-form :model="editForm" label-width="100px">
        <el-form-item label="记忆ID">
          <el-input v-model="editForm.memId" disabled />
        </el-form-item>
        <el-form-item label="记忆内容">
          <el-input v-model="editForm.content" type="textarea" :rows="6" placeholder="请输入记忆内容" />
        </el-form-item>
        <el-form-item label="变更原因">
          <el-input v-model="editForm.reason" type="textarea" :rows="3" placeholder="请说明修改原因" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSaveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新增/修改变量弹框（:8516 /update_variables/ upsert：新增时变量名可填，编辑时变量名锁定防改 key） -->
    <el-dialog v-model="varEditVisible" :title="varEditForm.mode === 'add' ? '新增变量' : '修改变量'" width="500px">
      <el-form :model="varEditForm" label-width="90px">
        <el-form-item label="变量名">
          <el-input v-model="varEditForm.name" :disabled="varEditForm.mode === 'edit'" placeholder="如 user_language" />
        </el-form-item>
        <el-form-item label="变量值">
          <el-input v-model="varEditForm.value" type="textarea" :rows="3" placeholder="变量值" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="varEditVisible = false">取消</el-button>
        <el-button type="primary" :loading="varSaving" @click="handleSaveVariable">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive } from 'vue'
import { listMemories, deleteMemory, updateMemory, batchDeleteMemories, getUserVariables, updateUserVariables, deleteUserVariables } from '@/api/memory'
import { getTracePage } from '@/api/trace'
import { getAllScopes } from '@/api/scope'
import type { PlatformMemoryItem } from '@/types/memory'
import type { ScopeRegistry } from '@/types/scope'
import { ElMessage, ElMessageBox } from 'element-plus'
import { InfoFilled } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { useRouter } from 'vue-router'

const userStore = useUserStore()
const router = useRouter()

const loading = ref(false)
const detailVisible = ref(false)
const editVisible = ref(false)
const saving = ref(false)
const currentMemory = ref<any>(null)
const selectedMemories = ref<any[]>([])
const traceData = ref<any>(null)
const traceLoading = ref(false)
const detailTab = ref('info')

const scopeOptions = ref<ScopeRegistry[]>([])

// 记忆列表筛选：scope 从平台拉取，userId 留空可查全部
const filter = reactive({
  scopeId: '',
  memoryType: '',
  userId: '',
})

const memories = ref<any[]>([])

const editForm = reactive({
  memId: '',
  content: '',
  oldContent: '',
  reason: '',
  userId: '',
  scopeId: '',
})

const pagination = reactive({
  page: 1,
  size: 20,
  total: 0,
})

// —— 变量 tab 状态 ——
const activeTab = ref<'memory' | 'variable'>('memory')
const varLoading = ref(false)
const varSaving = ref(false)
const varEditVisible = ref(false)
const variables = ref<{ name: string; value: string }[]>([])
const varFilter = reactive({
  scopeId: '',
  userId: '',
})
const varEditForm = reactive({
  mode: 'add' as 'add' | 'edit',
  name: '',
  value: '',
})

function getMemoryTypeLabel(type: string): string {
  const map: Record<string, string> = {
    user_profile: '画像',
    semantic_memory: '语义',
    episodic_memory: '情景',
    summary: '摘要',
  }
  return map[type] || type
}

function getMemoryTypeTag(type: string): string {
  const map: Record<string, string> = {
    user_profile: 'primary',
    semantic_memory: 'success',
    episodic_memory: 'warning',
    summary: 'info',
  }
  return map[type] || 'info'
}

// ISO 8601（如 2026-07-08T01:27:20+00:00）→ 本地可读 "YYYY-MM-DD HH:mm:ss"，解析失败原样返回
function formatTs(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function handleSizeChange() {
  pagination.page = 1
  fetchMemories()
}

function handlePageChange() {
  fetchMemories()
}

// 拉取平台 Scope 列表，根据用户角色显示对应的 Scope
async function fetchScopes() {
  try {
    const allScopes = await getAllScopes()
    
    // 全局角色（SUPER_ADMIN/PLATFORM_ADMIN/SECURITY_ADMIN）显示所有 Scope
    if (userStore.isSuperAdmin || userStore.isPlatformAdmin || userStore.isSecurityAdmin) {
      scopeOptions.value = allScopes
    } else {
      // 租户绑定角色（SCOPE_ADMIN/READ_ONLY/VIEWER）只显示绑定的 Scope
      scopeOptions.value = allScopes.filter(s => 
        userStore.scopeIds.includes(s.scopeId)
      )
    }
    
    if (scopeOptions.value.length > 0) {
      filter.scopeId = scopeOptions.value[0].scopeId
      varFilter.scopeId = filter.scopeId
      fetchMemories()
    } else {
      // 如果没有 Scope，清空筛选条件
      filter.scopeId = ''
      varFilter.scopeId = ''
      console.log('[Scope 过滤] 当前用户未绑定任何 Scope')
    }
  } catch (e) {
    // request 拦截器已提示
  }
}

// scope 下拉切换时同步变量 tab 的 scope
function onScopeChange(val: string) {
  varFilter.scopeId = val
}

// 对接平台 GET /api/v1/ops/memory（平台开放、暂不鉴权；平台→:8516 取数据）。
// 平台 MemoryItem 仅含 memId/content/type，scopeId/userId 用筛选条件回填。
async function fetchMemories() {
  loading.value = true
  try {
    const res = await listMemories({
      scope_id: filter.scopeId || undefined,
      user_id: filter.userId || undefined,
      memory_type: (filter.memoryType || undefined) as any,
      page_idx: pagination.page,
      page_size: pagination.size,
    })
    const items = res.items || []
    memories.value = items.map((m: PlatformMemoryItem) => ({
      memId: m.mem_id,
      content: m.content,
      type: m.type,
      scopeId: m.scope_id || filter.scopeId || '',
      scopeName: '',
      userId: m.user_id || filter.userId || '',
      timestamp: formatTs(m.timestamp),
      sourceId: m.source_id || '',
    }))

    // :8516 get_user_mem_by_page 现在返回真实 total
    pagination.total = res.total ?? 0
  } catch (e: any) {
    // 错误已由 request 拦截器提示
  } finally {
    loading.value = false
  }
}

function handleViewDetail(row: any) {
  currentMemory.value = row
  detailVisible.value = true
  // 同时加载追溯数据，传入记忆字段避免后端翻页查找
  traceData.value = null
  traceLoading.value = true
  getTracePage(row.memId, row.userId || undefined, row.scopeId || undefined, {
    content: row.content,
    type: row.type,
    timestamp: row.timestamp,
    source_id: row.sourceId,
  })
    .then((res: any) => { traceData.value = res })
    .catch(() => { traceData.value = null })
    .finally(() => { traceLoading.value = false })
}

function handleEdit(row: any) {
  editForm.memId = row.memId
  editForm.content = row.content
  editForm.oldContent = row.content
  editForm.reason = ''
  editForm.userId = row.userId
  editForm.scopeId = row.scopeId
  editVisible.value = true
}

async function handleSaveEdit() {
  if (!editForm.content) {
    ElMessage.warning('请输入记忆内容')
    return
  }
  if (!editForm.reason) {
    ElMessage.warning('请输入变更原因')
    return
  }
  saving.value = true
  try {
    await updateMemory(editForm.memId, editForm.content, editForm.userId, editForm.scopeId, editForm.reason, editForm.oldContent)
    ElMessage.success('记忆修改成功')
    editVisible.value = false
    // 更新详情页内容，不关闭侧边栏
    if (currentMemory.value && currentMemory.value.memId === editForm.memId) {
      currentMemory.value.content = editForm.content
      // 重新加载追溯数据以获取最新变更历史
      traceData.value = null
      traceLoading.value = true
      getTracePage(editForm.memId, editForm.userId || undefined, editForm.scopeId || undefined, {
        content: editForm.content,
        type: currentMemory.value.type,
        timestamp: currentMemory.value.timestamp,
        source_id: currentMemory.value.sourceId,
      })
        .then((res: any) => { traceData.value = res })
        .catch(() => { traceData.value = null })
        .finally(() => { traceLoading.value = false })
    }
    fetchMemories()
  } catch (e: any) {
    // 错误已由 request 拦截器提示
  } finally {
    saving.value = false
  }
}

async function handleDelete(row: any) {
  try {
    await ElMessageBox.confirm(`确定要删除记忆 "${row.memId}" 吗？此操作不可恢复。`, '警告', { type: 'warning' })
    await deleteMemory(row.memId, row.userId, row.scopeId, row.content)
    ElMessage.success('记忆删除成功')
    detailVisible.value = false
    fetchMemories()
  } catch (e: any) {
    // 取消删除或错误（错误已由拦截器提示）
  }
}

function handleSelectionChange(selection: any[]) {
  selectedMemories.value = selection
}

async function handleBatchDelete() {
  const ids = selectedMemories.value.map((m: any) => m.memId)
  try {
    await ElMessageBox.confirm(`确定要删除选中的 ${ids.length} 条记忆吗？此操作不可恢复。`, '警告', { type: 'warning' })
    await batchDeleteMemories(ids, filter.userId, filter.scopeId)
    ElMessage.success(`成功删除 ${ids.length} 条记忆`)
    selectedMemories.value = []
    fetchMemories()
  } catch (e: any) {
    // 取消或错误（错误已由拦截器提示）
  }
}

function handleViewTrace() {
  if (!currentMemory.value?.memId) return
  router.push({
    path: '/ops/trace',
    query: {
      memId: currentMemory.value.memId,
      userId: currentMemory.value.userId || '',
      scopeId: currentMemory.value.scopeId || '',
    },
  })
  detailVisible.value = false
}

// ============ 变量 CRUD ============

// 调 :8516 /get_variables/，返回 {name: value} map → 转 [{name,value}] 表格行
async function fetchVariables() {
  varLoading.value = true
  try {
    const map = await getUserVariables(varFilter.userId || undefined, varFilter.scopeId || undefined)
    console.log('[变量查询] GET 响应:', map, 'filter=', { ...varFilter })
    variables.value = Object.entries(map).map(([name, value]) => ({ name, value }))
  } catch (e: any) {
    console.error('[变量查询] 失败:', e)
    ElMessage.error('变量查询失败: ' + (e?.message || e))
  } finally {
    varLoading.value = false
  }
}

function handleAddVariable() {
  varEditForm.mode = 'add'
  varEditForm.name = ''
  varEditForm.value = ''
  varEditVisible.value = true
}

function handleEditVariable(row: { name: string; value: string }) {
  varEditForm.mode = 'edit'
  varEditForm.name = row.name
  varEditForm.value = row.value
  varEditVisible.value = true
}

async function handleSaveVariable() {
  if (!varEditForm.value) {
    ElMessage.warning('请输入变量值')
    return
  }
  varSaving.value = true
  try {
    // :8516 update_variables 按 {name: value} 合并写入（修改变量值）
    const payload = { [varEditForm.name]: varEditForm.value }
    console.log('[变量修改] PUT 请求:', {
      user_id: varFilter.userId || undefined,
      scope_id: varFilter.scopeId || undefined,
      variables: payload,
    })
    const res = await updateUserVariables(
      varFilter.userId || undefined,
      varFilter.scopeId || undefined,
      payload,
    )
    console.log('[变量修改] PUT 响应:', res)
    ElMessage.success('变量修改成功')
    varEditVisible.value = false
    await fetchVariables()
  } catch (e: any) {
    console.error('[变量修改] 失败:', e)
    // 错误已由 request 拦截器提示
  } finally {
    varSaving.value = false
  }
}

async function handleDeleteVariable(row: { name: string; value: string }) {
  try {
    await ElMessageBox.confirm(`确定要删除变量 "${row.name}" 吗？`, '警告', { type: 'warning' })
    await deleteUserVariables(
      varFilter.userId || undefined,
      varFilter.scopeId || undefined,
      [row.name],
    )
    ElMessage.success('变量删除成功')
    fetchVariables()
  } catch (e: any) {
    // 取消或错误（错误已由拦截器提示）
  }
}

onMounted(() => {
  fetchScopes()
})
</script>

<style scoped>
.mem-tabs {
  margin-bottom: 8px;
}

/* 内层标签页样式调整 */
.mem-tabs :deep(.el-tabs__header) {
  background: #FFFFFF;
  border-radius: 8px;
  padding: 0 16px;
  border: 1px solid #E5E7EB;
  margin-bottom: 16px !important;
}

.mem-tabs :deep(.el-tabs__content) {
  padding-top: 0;
}

/* 筛选栏增加顶部间距 */
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.hint-icon {
  font-size: 16px;
  color: #9CA3AF;
  cursor: help;
}

.pagination-bar {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}

.batch-actions {
  margin-top: 16px;
  padding: 12px;
  background: #FEF2F2;
  border: 1px solid #FECACA;
  border-radius: 8px;
  display: flex;
  justify-content: flex-end;
}

.empty-hint {
  margin-top: 24px;
  text-align: center;
  color: #9CA3AF;
  font-size: 13px;
}

/* —— 记忆详情侧边栏 —— */
.memory-detail-drawer .el-drawer__body { 
  padding: 0 20px;
}

.memory-detail { 
  display: flex; 
  flex-direction: column; 
  gap: 20px; 
}

.detail-header { 
  display: flex; 
  align-items: center; 
  gap: 12px; 
}

.detail-id { 
  font-size: 12px; 
  color: #9CA3AF; 
  font-family: 'JetBrains Mono', 'Consolas', monospace; 
  word-break: break-all; 
}

.detail-section { 
  margin-bottom: 8px; 
}

.detail-label { 
  font-size: 12px; 
  color: #6B7280; 
  margin-bottom: 6px; 
}

.detail-content-text {
  font-size: 14px; 
  line-height: 1.7; 
  color: #374151;
  word-break: break-all; 
  white-space: pre-wrap;
  background: #F9FAFB; 
  border-radius: 8px; 
  padding: 12px 16px;
  border: 1px solid #E5E7EB;
}

.detail-meta-list { 
  display: flex; 
  flex-direction: column; 
}

.detail-meta-row {
  display: flex; 
  align-items: center; 
  gap: 12px;
  padding: 8px 0; 
  border-bottom: 1px solid #F3F4F6;
}

.detail-meta-row:last-child { 
  border-bottom: none; 
}

.detail-value { 
  font-size: 14px; 
  color: #374151; 
}

.source-msg { 
  margin-bottom: 10px; 
  font-size: 13px; 
  line-height: 1.6; 
}

.source-msg-content { 
  color: #374151; 
}

.change-item { 
  display: flex; 
  flex-direction: column; 
  gap: 4px; 
}

.change-diff { 
  font-size: 13px; 
  color: #4B5563; 
}

.change-old { 
  color: #EF4444; 
}

.change-new { 
  color: #10B981; 
}

.change-reason { 
  font-size: 12px; 
  color: #9CA3AF; 
  font-style: italic; 
}
</style>
