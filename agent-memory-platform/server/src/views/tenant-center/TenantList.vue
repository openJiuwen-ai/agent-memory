<template>
  <div class="tenant-list">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>租户列表</span>
          <el-button type="primary" :icon="Plus" @click="openCreateDialog" v-if="userStore.hasPermission('tenant:write')">创建租户</el-button>
        </div>
      </template>

      <!-- 租户列表表格 -->
      <el-table :data="tenants" border v-loading="loading" style="width: 100%">
        <el-table-column prop="id" label="租户ID" min-width="220" show-overflow-tooltip />
        <el-table-column prop="name" label="租户名称" min-width="140" />
        <el-table-column label="Scope" min-width="240">
          <template #default="{ row }">
            <template v-if="row.scopeIds && row.scopeIds.length > 0">
              <el-tag type="info" size="small">{{ scopeLabel(row.scopeIds[0]) }}</el-tag>
            </template>
            <span v-else style="color: #999">未分配</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">
              {{ row.status === 'active' ? '活跃' : '已禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="createTime" label="创建时间" min-width="180" />
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link @click="handleViewDetail(row)">详情</el-button>
            <el-button type="warning" link @click="handleEdit(row)" :disabled="!userStore.hasPermission('tenant:write')">编辑</el-button>
            <el-button type="danger" link @click="handleDelete(row)" :disabled="!userStore.hasPermission('tenant:write')">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 创建/编辑租户弹框 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="600px">
      <el-form :model="form" label-width="120px">
        <el-form-item label="租户ID">
          <el-input v-model="form.id" disabled placeholder="自动生成" />
        </el-form-item>
        <el-form-item label="租户名称">
          <el-input v-model="form.name" placeholder="如: 项目A" />
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="form.status">
            <el-radio label="active">活跃</el-radio>
            <el-radio label="disabled">已禁用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="可选" />
        </el-form-item>
        <el-form-item label="分配Scope">
          <el-select
            v-model="form.scopeId"
            filterable
            placeholder="请选择Scope（可搜索）"
            style="width: 100%"
            :disabled="availableScopes.length === 0"
            clearable
          >
            <el-option
              v-for="scope in availableScopes"
              :key="scope.scopeId"
              :label="`${scope.scopeName} (${scope.scopeId})`"
              :value="scope.scopeId"
            />
          </el-select>
          <div v-if="availableScopes.length === 0" style="color: #f56c6c; font-size: 12px; margin-top: 4px">
            已无空闲scope_id
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 租户详情弹框 -->
    <el-dialog v-model="detailVisible" title="租户详情" width="700px">
      <el-descriptions :column="2" border v-if="currentTenant">
        <el-descriptions-item label="租户ID">{{ currentTenant.id }}</el-descriptions-item>
        <el-descriptions-item label="租户名称">{{ currentTenant.name }}</el-descriptions-item>
        <el-descriptions-item label="管理员">{{ currentTenant.adminName }}</el-descriptions-item>
        <el-descriptions-item label="管理员邮箱">{{ currentTenant.adminEmail }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="currentTenant.status === 'active' ? 'success' : 'info'" size="small">
            {{ currentTenant.status === 'active' ? '活跃' : '已禁用' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ currentTenant.createTime }}</el-descriptions-item>
        <el-descriptions-item label="Scope">
          <el-tag v-if="currentTenant.scopeIds && currentTenant.scopeIds.length > 0" type="info" size="small">
            {{ currentTenant.scopeIds[0] }}
          </el-tag>
          <span v-else style="color: #999">未分配</span>
        </el-descriptions-item>
        <el-descriptions-item label="用户数量">{{ currentTenant.userCount || 0 }}</el-descriptions-item>
        <el-descriptions-item label="备注" :span="2">{{ currentTenant.description || '无' }}</el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button type="primary" @click="handleEdit(currentTenant!)">编辑</el-button>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { getTenantList, createTenant, updateTenant, deleteTenant } from '@/api/tenant'
import { getAvailableScopes, getAllScopes } from '@/api/scope'
import { useUserStore } from '@/stores/user'
import type { Tenant } from '@/types/tenant'
import type { ScopeRegistry } from '@/types/scope'

const userStore = useUserStore()

const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const detailVisible = ref(false)
const isEdit = ref(false)
const currentTenant = ref<Tenant | null>(null)

const tenants = ref<Tenant[]>([])
const availableScopes = ref<ScopeRegistry[]>([])
const allScopes = ref<ScopeRegistry[]>([])

const scopeNameMap = computed(() => {
  const m = new Map<string, string>()
  for (const s of allScopes.value) m.set(s.scopeId, s.scopeName)
  return m
})

function scopeLabel(scopeId: string): string {
  const name = scopeNameMap.value.get(scopeId)
  return name ? `${name} (${scopeId})` : scopeId
}

const form = reactive({
  id: '',
  name: '',
  status: 'active' as 'active' | 'disabled',
  description: '',
  scopeId: '',  // 单个Scope ID
})

const dialogTitle = computed(() => isEdit.value ? '编辑租户' : '创建租户')

async function fetchTenants() {
  loading.value = true
  try {
    const result = await getTenantList({ page: 1, pageSize: 100 })
    tenants.value = result.list
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '获取租户列表失败')
  } finally {
    loading.value = false
  }
}

async function loadAvailableScopes() {
  try {
    const scopes = await getAvailableScopes()
    availableScopes.value = scopes
  } catch (e: any) {
    console.error('加载可用Scope失败:', e)
  }
}

async function openCreateDialog() {
  form.id = ''
  form.name = ''
  form.status = 'active'
  form.description = ''
  form.scopeId = ''
  isEdit.value = false
  
  // 加载可分配的Scope
  await loadAvailableScopes()
  
  dialogVisible.value = true
}

function handleViewDetail(row: Tenant) {
  currentTenant.value = row
  detailVisible.value = true
}

async function handleEdit(row: Tenant) {
  form.id = row.id
  form.name = row.name
  form.status = (row.status as 'disabled' | 'active') || 'active'
  form.description = row.description || ''
  form.scopeId = (row.scopeIds && row.scopeIds.length > 0) ? row.scopeIds[0] : ''

  isEdit.value = true
  detailVisible.value = false
  
  // 加载可分配的Scope
  await loadAvailableScopes()
  
  dialogVisible.value = true
}

async function handleSave() {
  if (!form.name) {
    ElMessage.warning('请填写租户名称')
    return
  }
  saving.value = true
  try {
    // 将单个scopeId转换为数组传给后端
    const scopeIdsArray = form.scopeId ? [form.scopeId] : []
    
    if (isEdit.value) {
      await updateTenant(form.id, {
        name: form.name,
        remark: form.description,
        status: form.status,
        scopeIds: scopeIdsArray,
      })
      ElMessage.success('租户更新成功')
    } else {
      await createTenant({
        name: form.name,
        remark: form.description,
        scopeIds: scopeIdsArray,
      })
      ElMessage.success('租户创建成功')
    }
    dialogVisible.value = false
    fetchTenants()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    saving.value = false
  }
}

async function handleDelete(row: Tenant) {
  try {
    await ElMessageBox.confirm(`确定要删除租户 "${row.name}" 吗？`, '提示', { type: 'warning' })
    await deleteTenant(row.id)
    ElMessage.success('租户删除成功')
    fetchTenants()
  } catch {
    // 取消删除
  }
}

onMounted(async () => {
  fetchTenants()
  try {
    allScopes.value = await getAllScopes()
  } catch (e) {
    console.error('加载Scope列表失败:', e)
  }
})
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
