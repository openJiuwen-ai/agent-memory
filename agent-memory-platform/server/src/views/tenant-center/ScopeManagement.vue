<template>
  <div class="scope-management">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>Scope管理</span>
          <!-- 创建Scope按钮：只有 scope:write 权限的用户可见 -->
          <el-button 
            v-if="userStore.hasPermission('scope:write')" 
            type="primary" 
            :icon="Plus" 
            @click="openCreateDialog"
          >
            创建Scope
          </el-button>
        </div>
      </template>

      <!-- Scope列表表格 -->
      <el-table :data="scopes" border v-loading="loading">
        <el-table-column prop="scopeId" label="Scope ID" width="180" />
        <el-table-column prop="scopeName" label="Scope名称" width="150" />
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'unassigned' ? 'info' : 'success'" size="small">
              {{ row.status === 'unassigned' ? '未分配' : '已分配' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="createdAt" label="创建时间" width="180" />
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <!-- 编辑按钮：只有 scope:write 权限的用户可见，且 SCOPE_ADMIN 只能编辑自己分配的 Scope -->
            <el-button 
              v-if="userStore.hasPermission('scope:write') && canEditScope(row)" 
              type="primary" 
              link 
              @click="handleEdit(row)"
            >
              编辑
            </el-button>
            <!-- 删除按钮：只有 scope:write 权限且 Scope 未分配的用户可见 -->
            <el-button 
              v-if="userStore.hasPermission('scope:write') && canDeleteScope(row)" 
              type="danger" 
              link 
              @click="handleDelete(row)"
              :disabled="row.status === 'assigned'"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 创建/编辑Scope弹框 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="600px">
      <el-form :model="form" label-width="120px">
        <el-form-item label="Scope ID">
          <el-input 
            v-model="form.scopeId" 
            :disabled="isEdit" 
            placeholder="不填写将自动生成" 
          />
        </el-form-item>
        <el-form-item label="Scope名称">
          <el-input v-model="form.scopeName" placeholder="如: Scope_01" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input 
            v-model="form.description" 
            type="textarea" 
            :rows="3" 
            placeholder="可选" 
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { getAllScopes, createScope, updateScope, deleteScope } from '@/api/scope'
import type { ScopeRegistry } from '@/types/scope'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const editingScope = ref<ScopeRegistry | null>(null)

const scopes = ref<ScopeRegistry[]>([])

const form = reactive({
  scopeId: '',
  scopeName: '',
  description: '',
})

const dialogTitle = computed(() => isEdit.value ? '编辑Scope' : '创建Scope')

// 检查用户是否可以编辑该Scope
function canEditScope(row: ScopeRegistry): boolean {
  // SUPER_ADMIN 可以编辑所有Scope
  if (userStore.isSuperAdmin) {
    return true
  }
  // SCOPE_ADMIN 只能编辑分配给自己的Scope
  if (userStore.isScopeAdmin) {
    return userStore.scopeIds.includes(row.scopeId)
  }
  // 其他有 scope:write 权限的角色（如 PLATFORM_ADMIN 理论上不应该有）
  return false
}

// 检查用户是否可以删除该Scope
function canDeleteScope(row: ScopeRegistry): boolean {
  // 只能删除未分配的Scope
  return row.status === 'unassigned' && canEditScope(row)
}

async function fetchScopes() {
  loading.value = true
  try {
    scopes.value = await getAllScopes()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '获取Scope列表失败')
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  form.scopeId = ''
  form.scopeName = ''
  form.description = ''
  isEdit.value = false
  dialogVisible.value = true
}

function handleEdit(row: ScopeRegistry) {
  form.scopeId = row.scopeId
  form.scopeName = row.scopeName
  form.description = row.description || ''
  isEdit.value = true
  editingScope.value = row
  dialogVisible.value = true
}

async function handleSave() {
  if (!form.scopeName) {
    ElMessage.warning('请填写Scope名称')
    return
  }
  
  saving.value = true
  try {
    if (isEdit.value && editingScope.value) {
      await updateScope(editingScope.value.scopeId, {
        scopeName: form.scopeName,
        description: form.description,
      })
      ElMessage.success('Scope更新成功')
    } else {
      await createScope({
        scopeId: form.scopeId || undefined,
        scopeName: form.scopeName,
        description: form.description,
      })
      ElMessage.success('Scope创建成功')
    }
    dialogVisible.value = false
    fetchScopes()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    saving.value = false
  }
}

async function handleDelete(row: ScopeRegistry) {
  try {
    await ElMessageBox.confirm(
      `确定要删除 Scope "${row.scopeName}" 吗？此操作不可恢复。`,
      '警告',
      { type: 'warning' }
    )
    
    await deleteScope(row.scopeId)
    ElMessage.success('Scope删除成功')
    fetchScopes()
  } catch {
    // 取消删除
  }
}

onMounted(() => {
  fetchScopes()
})
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
