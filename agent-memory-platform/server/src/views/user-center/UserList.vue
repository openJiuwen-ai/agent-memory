<template>
  <div class="user-list">
    <div class="header-bar">
      <el-button type="primary" @click="openCreateDialog" v-if="canCreateUser">创建用户</el-button>
    </div>
    
    <el-table :data="users" border>
      <el-table-column prop="username" label="用户名" width="140" />
      <el-table-column prop="role" label="角色" width="140">
        <template #default="{ row }">
          <el-tag>{{ row.role }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="所属租户" width="140">
        <template #default="{ row }">
          <span v-if="row.role === 'PLATFORM_ADMIN' || row.role === 'SECURITY_ADMIN'">所有租户</span>
          <span v-else-if="row.role === 'SCOPE_ADMIN' || row.role === 'READ_ONLY' || row.role === 'VIEWER'">{{ getTenantNameById(row.tenantId) }}</span>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="160" show-overflow-tooltip />
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button v-if="userStore.hasPermission('user:write')" type="primary" link @click="handleEdit(row)">编辑</el-button>
          <el-button v-if="userStore.hasPermission('user:write')" type="danger" link @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无用户数据" />
      </template>
    </el-table>

    <!-- 编辑用户弹框 -->
    <el-dialog v-model="editVisible" :title="isCreate ? '创建用户' : '编辑用户'" width="480px">
      <el-form :model="editForm" label-width="100px">
        <el-form-item label="用户名">
          <el-input v-model="editForm.username" :disabled="!isCreate" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item label="密码" v-if="isCreate">
          <el-input v-model="editForm.password" type="password" placeholder="请输入密码" />
        </el-form-item>
        <!-- 编辑时：根据权限显示不同的密码修改方式 -->
        <template v-else>
          <!-- SUPER_ADMIN修改其他用户，或SCOPE_ADMIN修改READ_ONLY：显示重置密码开关 -->
          <el-form-item label="重置密码" v-if="canResetOtherUserPassword">
            <el-switch v-model="isResetPassword" @change="handlePasswordSwitchChange" />
          </el-form-item>
          <el-form-item label="新密码" v-if="!isCreate && isResetPassword && canResetOtherUserPassword">
            <el-input v-model="editForm.password" type="password" placeholder="请输入新密码（管理员重置，不需要原密码）" />
          </el-form-item>
          <!-- 用户修改自己的密码：需要原密码验证 -->
          <el-form-item label="修改密码" v-if="canChangeOwnPassword">
            <el-switch v-model="isChangeOwnPassword" @change="handleOwnPasswordSwitchChange" />
          </el-form-item>
          <el-form-item label="原密码" v-if="!isCreate && isChangeOwnPassword">
            <el-input v-model="oldPassword" type="password" placeholder="请输入原密码" />
          </el-form-item>
          <el-form-item label="新密码" v-if="!isCreate && isChangeOwnPassword">
            <el-input v-model="editForm.password" type="password" placeholder="请输入新密码" />
          </el-form-item>
        </template>
        <el-form-item label="角色">
          <el-select v-model="editForm.role" style="width: 100%">
            <el-option
              v-for="roleOption in availableRoles"
              :key="roleOption.value"
              :label="roleOption.label"
              :value="roleOption.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="所属租户" v-if="editForm.role === 'SCOPE_ADMIN' || editForm.role === 'READ_ONLY' || editForm.role === 'VIEWER'">
          <el-select v-model="editForm.tenantId" :disabled="!isCreate" placeholder="选择租户" style="width: 100%" clearable @change="handleTenantChange">
            <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="Scope权限" v-if="editForm.role === 'SCOPE_ADMIN' || editForm.role === 'READ_ONLY' || editForm.role === 'VIEWER'">
          <div v-if="selectedTenantScope" style="color: #409eff; font-size: 14px">
            {{ selectedTenantScope.scopeName }} ({{ selectedTenantScope.scopeId }})
          </div>
          <span v-else class="text-muted">请先选择租户</span>
        </el-form-item>
        <el-form-item v-else-if="editForm.role === 'PLATFORM_ADMIN' || editForm.role === 'SECURITY_ADMIN'" label="Scope权限">
          <div style="color: #909399; font-size: 12px">
            PLATFORM_ADMIN和SECURITY_ADMIN拥有全局权限，无需指定Scope
          </div>
        </el-form-item>
        <el-form-item v-else label="Scope权限">
          <span class="text-muted">请先选择角色</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.remark" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getUserList, updateUser, createUser, resetUserPassword, deleteUser, changeMyPassword } from '@/api/users'
import { getTenantList } from '@/api/tenant'
import { getAllScopes, getScopesByTenantId } from '@/api/scope'
import { useUserStore } from '@/stores/user'
import type { UserRole, Tenant } from '@/types/tenant'
import type { ScopeRegistry } from '@/types/scope'

const userStore = useUserStore()

// 计算当前用户角色
const currentUserRole = computed(() => userStore.role || 'READ_ONLY')
const currentUserScopeIds = computed(() => userStore.scopeIds || [])

// 计算可创建的角色选项
const availableRoles = computed(() => {
  const role = currentUserRole.value
  if (role === 'SUPER_ADMIN') {
    return [
      { label: 'PLATFORM_ADMIN', value: 'PLATFORM_ADMIN' },
      { label: 'SECURITY_ADMIN', value: 'SECURITY_ADMIN' },
      { label: 'SCOPE_ADMIN', value: 'SCOPE_ADMIN' },
      { label: 'READ_ONLY', value: 'READ_ONLY' },
      { label: 'VIEWER', value: 'VIEWER' },
    ]
  } else if (role === 'SCOPE_ADMIN') {
    return [
      { label: 'READ_ONLY', value: 'READ_ONLY' },
      { label: 'VIEWER', value: 'VIEWER' },
    ]
  }
  // PLATFORM_ADMIN和SECURITY_ADMIN不能创建用户
  return []
})

// 计算是否显示创建按钮
const canCreateUser = computed(() => availableRoles.value.length > 0)

const users = ref<any[]>([])
const editVisible = ref(false)
const isCreate = ref(false)
const saving = ref(false)
const tenants = ref<Tenant[]>([])
const allScopes = ref<ScopeRegistry[]>([])  // 所有scope列表

const editForm = reactive({
  id: '',
  username: '',
  password: '',
  role: '' as UserRole,
  tenantId: '',
  scopeIds: [] as string[],
  remark: '',
})

const changePassword = ref(false)  // 是否修改密码
const isResetPassword = ref(false)  // 是否重置密码（管理员操作）
const isChangeOwnPassword = ref(false)  // 是否修改自己的密码
const oldPassword = ref('')  // 原密码

// 计算属性：是否可以重置其他用户的密码
const canResetOtherUserPassword = computed(() => {
  if (isCreate.value) return false
  const currentRole = currentUserRole.value
  const targetRole = editForm.role
  const targetUsername = editForm.username
  const currentUsername = userStore.username
  
  // SUPER_ADMIN可以修改其他所有用户的密码（不能修改自己）
  if (currentRole === 'SUPER_ADMIN' && targetUsername !== currentUsername) {
    return true
  }
  
  // SCOPE_ADMIN可以修改READ_ONLY和VIEWER用户的密码
  if (currentRole === 'SCOPE_ADMIN' && (targetRole === 'READ_ONLY' || targetRole === 'VIEWER')) {
    return true
  }
  
  return false
})

// 计算属性：是否可以修改自己的密码
const canChangeOwnPassword = computed(() => {
  if (isCreate.value) return false
  const currentUsername = userStore.username
  const targetUsername = editForm.username
  
  // 只能修改自己的密码
  return currentUsername === targetUsername
})

// 计算属性：根据选中的租户自动获取对应的Scope
const selectedTenantScope = computed(() => {
  if (!editForm.tenantId) return null
  const tenant = tenants.value.find(t => t.id === editForm.tenantId)
  if (!tenant || !tenant.scopeIds || tenant.scopeIds.length === 0) return null
  
  // 租户与Scope是一对一关系，取第一个Scope
  const scopeId = tenant.scopeIds[0]
  return allScopes.value.find(s => s.scopeId === scopeId) || null
})

async function fetchUsers() {
  users.value = await getUserList()
}

async function fetchTenants() {
  const result = await getTenantList()
  tenants.value = result.list
}

// 根据租户ID获取租户名称
function getTenantNameById(tenantId: string): string {
  const tenant = tenants.value.find(t => t.id === tenantId)
  return tenant ? tenant.name : '-'
}

async function fetchAllScopes() {
  try {
    const scopes = await getAllScopes()
    allScopes.value = scopes
  } catch (e: any) {
    console.error('加载Scope列表失败:', e)
  }
}

function openCreateDialog() {
  isCreate.value = true
  changePassword.value = false
  isResetPassword.value = false
  editForm.id = ''
  editForm.username = ''
  editForm.password = ''
  editForm.role = 'READ_ONLY'
  editForm.tenantId = ''
  editForm.scopeIds = []
  editForm.remark = ''
  editVisible.value = true
}

function handlePasswordSwitchChange(val: boolean) {
  if (!val) {
    editForm.password = ''  // 关闭重置密码时清空密码框
  }
  // 关闭重置密码时，也关闭自己密码修改
  isChangeOwnPassword.value = false
  oldPassword.value = ''
}

function handleOwnPasswordSwitchChange(val: boolean) {
  if (!val) {
    editForm.password = ''
    oldPassword.value = ''
  }
  // 关闭自己密码修改时，也关闭重置密码
  isResetPassword.value = false
}

function handleTenantChange() {
  // 当选择租户时，自动设置该租户对应的 Scope
  const selectedTenant = tenants.value.find(t => t.id === editForm.tenantId)
  
  if (selectedTenant && selectedTenant.scopeIds && selectedTenant.scopeIds.length > 0) {
    // 租户与 Scope 是一对一关系，直接取第一个 Scope
    editForm.scopeIds = [selectedTenant.scopeIds[0]]
  } else {
    // 租户没有分配 Scope
    editForm.scopeIds = []
  }
}

function handleEdit(row: any) {
  isCreate.value = false
  changePassword.value = false  // 重置修改密码开关
  isResetPassword.value = false
  isChangeOwnPassword.value = false
  oldPassword.value = ''
  editForm.id = row.id
  editForm.username = row.username
  editForm.password = ''  // 编辑时不显示旧密码
  editForm.role = row.role
  editForm.tenantId = row.tenantId || ''
  editForm.scopeIds = row.scopeIds || []
  editForm.remark = row.remark
  
  // 加载该租户下的Scope
  if (editForm.tenantId) {
    const selectedTenant = tenants.value.find(t => t.id === editForm.tenantId)
    if (selectedTenant && selectedTenant.scopeIds && selectedTenant.scopeIds.length > 0) {
      editForm.scopeIds = [selectedTenant.scopeIds[0]]
    } else {
      editForm.scopeIds = []
    }
  } else {
    editForm.scopeIds = []
  }
  
  editVisible.value = true
}

async function handleSave() {
  if (!editForm.username) {
    ElMessage.warning('请输入用户名')
    return
  }
  if (isCreate.value && !editForm.password) {
    ElMessage.warning('请输入密码')
    return
  }
  // 密码长度校验：至少 6 位
  if (isCreate.value && editForm.password && editForm.password.length < 6) {
    ElMessage.warning('密码长度不能少于 6 位')
    return
  }
  
  console.log('[用户创建] 准备保存，form data:', {
    username: editForm.username,
    role: editForm.role,
    tenantId: editForm.tenantId,
    scopeIds: editForm.scopeIds,
  })
  
  saving.value = true
  try {
    if (isCreate.value) {
      // 根据角色决定是否传递 tenant_id
      const isGlobalRole = editForm.role === 'PLATFORM_ADMIN' || editForm.role === 'SECURITY_ADMIN'
        
      await createUser({
        username: editForm.username,
        password: editForm.password,
        role: editForm.role,
        tenant_id: isGlobalRole ? null : (editForm.tenantId || null),
        scopeIds: isGlobalRole ? null : editForm.scopeIds,
        remark: editForm.remark,
      })
      ElMessage.success('用户创建成功')
    } else {
      // 编辑用户：处理密码修改
      if (isChangeOwnPassword.value) {
        // 修改自己的密码：需要验证原密码
        if (!oldPassword.value) {
          ElMessage.warning('请输入原密码')
          saving.value = false
          return
        }
        if (!editForm.password) {
          ElMessage.warning('请输入新密码')
          saving.value = false
          return
        }
        
        // 调用修改自己密码的接口（需要原密码验证）
        await changeMyPassword(editForm.id, oldPassword.value, editForm.password)
        ElMessage.success('密码修改成功')
      } else if (isResetPassword.value && editForm.password) {
        // 管理员重置其他用户密码：至少 6 位
        if (editForm.password.length < 6) {
          ElMessage.warning('密码长度不能少于 6 位')
          saving.value = false
          return
        }
        await resetUserPassword(editForm.id, editForm.password)
        ElMessage.success('用户信息已更新，密码已重置')
      } else {
        // 只更新基本信息
        await updateUser(editForm.id, {
          username: editForm.username,
          role: editForm.role,
          scopeIds: editForm.scopeIds,
          remark: editForm.remark,
        })
        ElMessage.success('用户信息已更新')
      }
    }
    editVisible.value = false
    fetchUsers()
  } catch (e: any) {
    // 错误已由 request.ts 拦截器处理，不需要重复提示
  } finally {
    saving.value = false
  }
}

async function handleDelete(row: any) {
  try {
    await ElMessageBox.confirm(
      `确定要删除用户 "${row.username}" 吗？`,
      '警告',
      { type: 'warning' }
    )
    
    // 检查row.id是否存在
    if (!row.id) {
      ElMessage.error('用户ID不存在，无法删除')
      return
    }
    
    await deleteUser(row.id)
    ElMessage.success('用户删除成功')
    fetchUsers()
  } catch (error: any) {
    // ElMessageBox.confirm 取消时会关闭对话框，不显示错误
    if (error !== 'cancel' && error?.toString() !== 'cancel') {
      console.error('删除用户失败:', error)
      ElMessage.error(error?.response?.data?.message || error?.message || '删除失败')
    }
  }
}

onMounted(() => {
  fetchUsers()
  fetchTenants()
  fetchAllScopes()  // 加载所有Scope列表
})
</script>

<style scoped>
.header-bar {
  margin-bottom: 16px;
}
.text-muted { color: #909399; font-size: 13px; }
</style>
