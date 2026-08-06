<template>
  <div v-loading="loading" class="tenant-detail">
    <el-button v-if="showBack" :icon="ArrowLeft" style="margin-bottom: 16px" @click="$emit('back')">
      返回租户列表
    </el-button>

    <template v-if="detail">
      <el-card class="info-card">
        <template #header>
          <div class="card-header">
            <span>租户信息</span>
            <el-button v-if="showAdminControls" link type="primary" @click="openTenantEditDialog">
              修改租户信息
            </el-button>
          </div>
        </template>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="租户名称">{{ detail.name }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ detail.createTime }}</el-descriptions-item>
          <el-descriptions-item label="备注" :span="2">{{ detail.remark || '—' }}</el-descriptions-item>
          <el-descriptions-item label="管理员账号">{{ detail.admin.username }}</el-descriptions-item>
          <el-descriptions-item label="管理员备注">{{ detail.admin.remark || '—' }}</el-descriptions-item>
          <el-descriptions-item label="管理员 scope-id" :span="2">
            <el-tag v-for="s in detail.admin.scopeIds" :key="s" size="small" style="margin: 2px">
              {{ s }}
            </el-tag>
            <span v-if="detail.admin.scopeIds.length === 0" class="text-muted">未分配</span>
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <!-- 2026-07-17 P0-3 v2：租户级 Scope 配置（1 tenant = 1 scope） -->
      <el-card class="scope-config-card">
        <template #header>
          <div class="card-header">
            <span>配置快照（1 tenant = 1 scope）</span>
            <div>
              <el-tag
                v-if="scopeConfig && scopeConfig.isDeviated"
                size="small"
                type="warning"
              >
                已偏离模板
              </el-tag>
              <el-button
                size="small"
                style="margin-left: 8px"
                @click="loadScopeConfig"
                :loading="loadingScope"
              >
                刷新
              </el-button>
            </div>
          </div>
        </template>

        <template v-if="scopeConfig">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="绑定模板">
              {{ scopeConfig.templateName || '未绑定' }}
            </el-descriptions-item>
            <el-descriptions-item label="模板版本 / 当前版本">
              <span v-if="scopeConfig.templateVersion != null">
                模板 v{{ scopeConfig.templateVersion }} / 当前 v{{ scopeConfig.currentVersion }}
                <el-tag
                  v-if="scopeConfig.isDeviated"
                  size="small"
                  type="warning"
                  style="margin-left: 6px"
                >
                  偏离
                </el-tag>
              </span>
              <span v-else>—</span>
            </el-descriptions-item>
            <el-descriptions-item label="最近修改人">
              {{ scopeConfig.updatedBy || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="最近修改时间">
              {{ scopeConfig.updatedAt || '—' }}
            </el-descriptions-item>
          </el-descriptions>

          <el-form label-width="120px" style="margin-top: 16px">
            <el-form-item label="配置 JSON">
              <el-input
                v-model="configDraft"
                type="textarea"
                :rows="12"
                :disabled="!showAdminControls && !showTenantModify"
              />
              <div class="hint" v-if="scopeConfig.isDeviated && showTenantModify">
                当前配置已偏离模板（模板 v{{ scopeConfig.templateVersion }}，当前 v{{ scopeConfig.currentVersion }}），
                可手动同步回模板，或继续自定义保存
              </div>
            </el-form-item>
            <el-form-item label="变更原因">
              <el-input
                v-model="reasonDraft"
                placeholder="（审计必填）说明本次修改"
                :disabled="!showAdminControls && !showTenantModify"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="savingConfig"
                @click="onSaveScopeConfig"
                :disabled="!showAdminControls && !showTenantModify"
              >
                保存参数
              </el-button>
              <el-button
                v-if="scopeConfig.isDeviated"
                @click="onSyncFromTemplate"
                :loading="syncingFromTemplate"
                :disabled="!showAdminControls"
              >
                同步回模板
              </el-button>
              <el-button @click="openApplyTemplateDialog">应用模板…</el-button>
            </el-form-item>
          </el-form>
        </template>

        <el-empty v-else description="尚未创建 Scope 配置" />
      </el-card>

      <el-card class="member-card">
        <template #header>
          <div class="card-header">
            <span>成员列表（普通用户 / 访客）</span>
            <el-button type="success" @click="openMemberDialog()">新增成员</el-button>
          </div>
        </template>
        <el-table :data="members" border>
          <el-table-column prop="username" label="用户名" min-width="140" />
          <el-table-column label="角色" width="110">
            <template #default="{ row }">
              <el-tag :type="row.role === 'guest' ? 'info' : 'primary'">
                {{ row.role === 'guest' ? '访客' : '普通用户' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="scope-id" min-width="200">
            <template #default="{ row }">
              <el-tag v-for="s in row.scopeIds" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="remark" label="备注" min-width="140" show-overflow-tooltip />
          <el-table-column prop="createTime" label="创建时间" width="170" />
          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openMemberDialog(row)">修改</el-button>
              <el-button link type="danger" @click="handleDeleteMember(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="members.length === 0" description="暂无成员" />
      </el-card>
    </template>

    <!-- 修改租户信息 -->
    <el-dialog v-model="tenantEditVisible" title="修改租户信息" width="520px">
      <el-form label-width="120px">
        <el-form-item label="租户名称">
          <el-input :model-value="detail?.name" disabled />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="tenantEditForm.remark" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="管理员 scope-id">
          <el-select v-model="tenantEditForm.adminScopeIds" multiple style="width: 100%">
            <el-option v-for="s in adminScopeOptions" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="修改密码">
          <el-switch v-model="tenantEditForm.changePassword" />
        </el-form-item>
        <el-form-item v-if="tenantEditForm.changePassword" label="新密码">
          <el-input v-model="tenantEditForm.newPassword" type="password" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="tenantEditVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleUpdateTenant">确定</el-button>
      </template>
    </el-dialog>

    <!-- 应用模板到本租户 -->
    <el-dialog v-model="applyDialogVisible" title="应用模板" width="520px">
      <el-form label-width="100px">
        <el-form-item label="选择模板" required>
          <el-select v-model="applyForm.templateId" filterable style="width: 100%">
            <el-option
              v-for="t in scopeTemplates"
              :key="t.id"
              :label="`${t.display_name || t.template_name} [${t.template_type}]`"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="变更原因" required>
          <el-input v-model="applyForm.reason" placeholder="（审计必填）说明本次应用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="applyDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="applyingTemplate" @click="onApplyTemplate">确认应用</el-button>
      </template>
    </el-dialog>

    <!-- 新增/修改成员 -->
    <el-dialog v-model="memberVisible" :title="memberForm.id ? '修改成员' : '新增成员'" width="480px">
      <el-form ref="memberFormRef" :model="memberForm" :rules="memberRules" label-width="100px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="memberForm.username" :disabled="!!memberForm.id" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item :label="memberForm.id ? '重置密码' : '密码'" :prop="memberForm.id ? '' : 'password'">
          <el-input
            v-model="memberForm.password"
            type="password"
            show-password
            :placeholder="memberForm.id ? '留空表示不修改密码' : '请输入密码'"
          />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="memberForm.role" style="width: 100%">
            <el-option label="普通用户" value="tenant_user" />
            <el-option label="访客" value="guest" />
          </el-select>
        </el-form-item>
        <el-form-item label="scope-id" prop="scopeIds">
          <el-select v-model="memberForm.scopeIds" multiple style="width: 100%" placeholder="不超过管理员权限范围">
            <el-option v-for="s in adminScopeOptions" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="memberForm.remark" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmitMember">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import {
  createTenantMember,
  deleteTenantMember,
  getTenantDetail,
  getTenantMembers,
  updateTenant,
  updateTenantMember,
} from '@/api/tenant'
import {
  getTenantScopeConfig,
  updateTenantScopeConfig,
  syncTenantFromTemplate,
} from '@/api/tenant-scope-config'
import { listTemplates, applyTemplate } from '@/api/template'
import type { TenantAccount, TenantDetail as TenantDetailType, TenantMemberForm, TenantUpdateForm, TenantMember } from '@/types/tenant'
import type { TenantScopeConfig, Template } from '@/types/config'

const props = defineProps<{
  tenantId: string
  showAdminControls: boolean
  showBack: boolean
  /** 租户登录用户可修改自己的参数（2026-07-17 P0-3 v2 决策） */
  showTenantModify?: boolean
}>()
defineEmits<{ (e: 'back'): void }>()

const loading = ref(false)
const submitting = ref(false)
const detail = ref<TenantDetailType | null>(null)
const members = ref<TenantMember[]>([])

const adminScopeOptions = computed(() => detail.value?.admin.scopeIds ?? [])

async function fetchDetail() {
  loading.value = true
  try {
    detail.value = (await getTenantDetail(props.tenantId)) as any
    members.value = await getTenantMembers(props.tenantId)
  } finally {
    loading.value = false
  }
}

/* ---------------- 租户级 Scope 配置（1 tenant = 1 scope） ---------------- */

const scopeConfig = ref<TenantScopeConfig | null>(null)
const configDraft = ref<string>('{}')
const configSnapshot = ref<string>('{}')
const reasonDraft = ref<string>('')
const loadingScope = ref(false)
const savingConfig = ref(false)
const syncingFromTemplate = ref(false)

const loadScopeConfig = async () => {
  loadingScope.value = true
  try {
    const r = await getTenantScopeConfig(props.tenantId)
    scopeConfig.value = r
    configDraft.value = r.configJson ?? ''
    configSnapshot.value = r.configJson ?? ''
  } catch (e: any) {
    // 404 = 尚未创建（正常状态）
    if (e?.response?.status === 404) {
      scopeConfig.value = null
    } else {
      ElMessage.error('加载 Scope 配置失败: ' + e.message)
    }
  } finally {
    loadingScope.value = false
  }
}

const onSaveScopeConfig = async () => {
  if (!reasonDraft.value.trim()) {
    ElMessage.warning('请填写变更原因（审计必填）')
    return
  }
  if (configDraft.value === configSnapshot.value) {
    ElMessage.warning('配置未变更')
    return
  }
  try {
    JSON.parse(configDraft.value)
  } catch {
    ElMessage.error('配置不是合法 JSON')
    return
  }
  savingConfig.value = true
  try {
    const r = await updateTenantScopeConfig(props.tenantId, configDraft.value)
    scopeConfig.value = r
    configSnapshot.value = r.configJson ?? ''
    reasonDraft.value = ''
    ElMessage.success(
      r.isDeviated
        ? '已保存。当前配置与模板 v' + r.templateVersion + ' 偏离'
        : '已保存。与模板保持一致'
    )
  } catch (e: any) {
    ElMessage.error('保存失败: ' + e.message)
  } finally {
    savingConfig.value = false
  }
}

const onSyncFromTemplate = async () => {
  try {
    await ElMessageBox.confirm('确认把当前租户配置同步回模板最新版本？', '同步', { type: 'warning' })
  } catch (e) {
    if (e === 'cancel') return
  }
  syncingFromTemplate.value = true
  try {
    const r = await syncTenantFromTemplate(props.tenantId)
    scopeConfig.value = r
    configDraft.value = r.configJson ?? ''
    configSnapshot.value = r.configJson ?? ''
    ElMessage.success('已同步回模板 v' + r.templateVersion)
  } catch (e: any) {
    ElMessage.error('同步失败: ' + e.message)
  } finally {
    syncingFromTemplate.value = false
  }
}

/* ---------------- 应用模板到本租户（弹窗） ---------------- */

const applyDialogVisible = ref(false)
const applyingTemplate = ref(false)
const applyForm = reactive({ templateId: '', reason: '' })
const scopeTemplates = ref<Template[]>([])

const openApplyTemplateDialog = async () => {
  applyForm.templateId = ''
  applyForm.reason = ''
  applyDialogVisible.value = true
  try {
    scopeTemplates.value = await listTemplates('SCOPE', undefined)
  } catch (e: any) {
    ElMessage.error('加载模板失败: ' + e.message)
  }
}

const onApplyTemplate = async () => {
  if (!applyForm.templateId) {
    ElMessage.warning('请选择模板')
    return
  }
  if (!applyForm.reason.trim()) {
    ElMessage.warning('请填写变更原因')
    return
  }
  applyingTemplate.value = true
  try {
    await applyTemplate(applyForm.templateId, {
      targetTenantIds: [props.tenantId],
      reason: applyForm.reason,
    })
    ElMessage.success('已应用')
    applyDialogVisible.value = false
    // 同步刷新租户详情与 scope 配置：apply 会改写 template_id，
    // 不刷新会让顶部"当前模板"和跨页共享的租户状态停留在应用前的值
    await Promise.all([loadScopeConfig(), fetchDetail()])
  } catch (e: any) {
    ElMessage.error('应用失败: ' + e.message)
  } finally {
    applyingTemplate.value = false
  }
}

/* ---------------- 修改租户信息 ---------------- */

const tenantEditVisible = ref(false)
const tenantEditForm = reactive<TenantUpdateForm>({
  id: '',
  remark: '',
  adminScopeIds: [],
  changePassword: false,
  newPassword: '',
})

function openTenantEditDialog() {
  if (!detail.value) return
  tenantEditForm.id = detail.value.id
  tenantEditForm.remark = detail.value.remark
  tenantEditForm.adminScopeIds = [...detail.value.admin.scopeIds]
  tenantEditForm.changePassword = false
  tenantEditForm.newPassword = ''
  tenantEditVisible.value = true
}

async function handleUpdateTenant() {
  submitting.value = true
  try {
    await updateTenant(props.tenantId, { ...tenantEditForm } as any)
    ElMessage.success('租户信息已更新')
    tenantEditVisible.value = false
    fetchDetail()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '更新失败')
  } finally {
    submitting.value = false
  }
}

/* ---------------- 成员管理 ---------------- */

const memberVisible = ref(false)
const memberFormRef = ref<FormInstance>()
const memberForm = reactive<TenantMemberForm>({
  id: undefined,
  tenantId: props.tenantId,
  username: '',
  password: '',
  role: 'SCOPE_ADMIN',
  scopeIds: [],
  remark: '',
})

const memberRules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

function openMemberDialog(row?: TenantAccount) {
  if (row) {
    memberForm.id = row.id
    memberForm.username = row.username
    memberForm.password = ''
    memberForm.role = row.role as 'READ_ONLY' | 'SCOPE_ADMIN'
    memberForm.scopeIds = [...row.scopeIds]
    memberForm.remark = row.remark
  } else {
    memberForm.id = undefined
    memberForm.username = ''
    memberForm.password = ''
    memberForm.role = 'SCOPE_ADMIN'
    memberForm.scopeIds = []
    memberForm.remark = ''
  }
  memberVisible.value = true
}

async function handleSubmitMember() {
  if (!memberFormRef.value) return
  await memberFormRef.value.validate(async (valid) => {
    if (!valid) return
    submitting.value = true
    try {
      if (memberForm.id) {
        await updateTenantMember(props.tenantId, memberForm.id, { ...memberForm })
        ElMessage.success('成员信息已更新')
      } else {
        await createTenantMember(props.tenantId, { ...memberForm })
        ElMessage.success('成员创建成功')
      }
      memberVisible.value = false
      fetchDetail()
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '操作失败')
    } finally {
      submitting.value = false
    }
  })
}

async function handleDeleteMember(row: TenantAccount) {
  try {
    await ElMessageBox.confirm(`确定要删除成员「${row.username}」吗？`, '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await deleteTenantMember(props.tenantId, row.id)
    ElMessage.success('成员已删除')
    fetchDetail()
  } catch {
    // 取消删除
  }
}

watch(
  () => props.tenantId,
  () => {
    fetchDetail()
    loadScopeConfig()
  }
)

onMounted(() => {
  fetchDetail()
  loadScopeConfig()
})
</script>

<style scoped>
.info-card,
.scope-config-card,
.member-card {
  margin-bottom: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.text-muted {
  color: #909399;
  font-size: 13px;
}

.hint {
  font-size: 11px;
  color: #999;
  margin-top: 4px;
}
</style>
