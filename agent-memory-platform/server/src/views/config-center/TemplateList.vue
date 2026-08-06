<!-- 配置模板列表页：查看/编辑统一跳转到全屏模板页 -->
<template>
  <div class="template-list">
    <el-card>
      <el-tabs v-model="activeSubTab" class="sub-tabs">
        <el-tab-pane label="系统默认" name="builtin">
          <div class="toolbar">
            <el-select v-model="filterType" placeholder="类型过滤" clearable style="width: 160px" @change="loadList">
              <el-option label="SCOPE" value="SCOPE" />
              <el-option label="INSTANCE" value="INSTANCE" />
            </el-select>
          </div>
          <el-table :data="list" v-loading="loading" stripe>
            <el-table-column label="模板名" min-width="180">
              <template #default="{ row }">
                <el-link type="primary" :underline="false" @click="openDetailPage(row)">
                  {{ row.template_name }}
                </el-link>
                <el-tag size="small" type="warning" style="margin-left: 6px">预置</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="类型" min-width="120">
              <template #default="{ row }">
                <el-tag :type="row.template_type === 'SCOPE' ? 'primary' : 'danger'">{{ row.template_type }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="描述" min-width="200">
              <template #default="{ row }">
                <span class="desc">{{ row.description || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.status === 'draft'" type="warning" size="small">草稿</el-tag>
                <el-tag v-else type="success" size="small">已发布</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="使用租户" min-width="280" v-if="hasScopeType">
              <template #default="{ row }">
                <span v-if="row.template_type === 'SCOPE'">
                  <template v-if="row.tenant_usage?.length">
                    <el-tag
                      v-for="tenant in row.tenant_usage.slice(0, 3)"
                      :key="tenant.tenantId"
                      size="small"
                      style="margin-right: 6px; margin-bottom: 4px"
                    >
                      {{ tenant.tenantName || tenant.tenantId }}
                    </el-tag>
                    <el-link
                      v-if="row.tenant_usage.length > 3"
                      type="primary"
                      :underline="false"
                      @click="openUsageDialog(row)"
                    >
                      过多（{{ row.tenant_usage.length }}）
                    </el-link>
                  </template>
                  <span v-else>未使用</span>
                </span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" min-width="260" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="onApply(row)">应用</el-button>
                <el-button v-if="row.template_type === 'SCOPE' && row.tenant_usage?.length" size="small" @click="openTenantDrawer(row)">
                  管理租户
                </el-button>
                <el-button size="small" @click="openEditPage(row)">编辑</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="自定义" name="custom">
          <div class="toolbar">
            <el-select v-model="filterType" placeholder="类型过滤" clearable style="width: 160px" @change="loadList">
              <el-option label="SCOPE" value="SCOPE" />
              <el-option label="INSTANCE" value="INSTANCE" />
            </el-select>
            <el-button type="primary" style="margin-left: 12px" @click="onCreate">
              <el-icon><Plus /></el-icon>
              新建模板
            </el-button>
          </div>
          <el-table :data="list" v-loading="loading" stripe>
            <el-table-column label="模板名" min-width="180">
              <template #default="{ row }">
                <el-link type="primary" :underline="false" @click="openDetailPage(row)">
                  {{ row.template_name }}
                </el-link>
              </template>
            </el-table-column>
            <el-table-column label="类型" min-width="120">
              <template #default="{ row }">
                <el-tag :type="row.template_type === 'SCOPE' ? 'primary' : 'danger'">{{ row.template_type }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="描述" min-width="200">
              <template #default="{ row }">
                <span class="desc">{{ row.description || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.status === 'draft'" type="warning" size="small">草稿</el-tag>
                <el-tag v-else type="success" size="small">已发布</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="使用租户" min-width="280" v-if="hasScopeType">
              <template #default="{ row }">
                <span v-if="row.template_type === 'SCOPE'">
                  <template v-if="row.tenant_usage?.length">
                    <el-tag
                      v-for="tenant in row.tenant_usage.slice(0, 3)"
                      :key="tenant.tenantId"
                      size="small"
                      style="margin-right: 6px; margin-bottom: 4px"
                    >
                      {{ tenant.tenantName || tenant.tenantId }}
                    </el-tag>
                    <el-link
                      v-if="row.tenant_usage.length > 3"
                      type="primary"
                      :underline="false"
                      @click="openUsageDialog(row)"
                    >
                      过多（{{ row.tenant_usage.length }}）
                    </el-link>
                  </template>
                  <span v-else>未使用</span>
                </span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" min-width="320" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="onApply(row)">应用</el-button>
                <el-button v-if="row.template_type === 'SCOPE' && row.tenant_usage?.length" size="small" @click="openTenantDrawer(row)">
                  管理租户
                </el-button>
                <el-button size="small" @click="openCopyPage(row)">复制</el-button>
                <el-button size="small" @click="openEditPage(row)">编辑</el-button>
                <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-dialog v-model="applyDialogVisible" :title="`应用模板 — ${currentTemplate?.template_name}`" width="640px">
      <el-form label-width="100px" v-if="currentTemplate">
        <el-form-item v-if="currentTemplate.template_type === 'SCOPE'" label="目标租户" required>
          <el-select
            v-model="applyForm.targetTenantIds"
            multiple
            filterable
            placeholder="选择要应用的租户（必填）"
            style="width: 100%"
          >
            <el-option v-for="t in applicableTenants" :key="t.id" :value="t.id">
              <span>{{ t.name }}</span>
              <span v-if="t.currentTemplateName" style="float: right; color: #8492a6; font-size: 13px">
                当前: {{ t.currentTemplateName }}
              </span>
              <span v-else style="float: right; color: #c0c4cc; font-size: 13px">未应用模板</span>
            </el-option>
          </el-select>
          <div class="hint">热生效，立即同步到目标租户</div>
        </el-form-item>
        <el-form-item v-else label="应用提示">
          <el-alert type="warning" :closable="false">
            INSTANCE 模板会写入全局参数，实际效果需刷新服务后生效。
          </el-alert>
        </el-form-item>
        <el-form-item label="变更原因">
          <el-input v-model="applyForm.reason" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="applyDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmApply" :loading="applying">确认应用</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="usageDialogVisible" :title="`使用「${currentTemplate?.template_name}」的租户`" width="520px">
      <el-table :data="usageTenants" v-loading="usageLoading">
        <el-table-column label="租户名" prop="tenantName" min-width="180" />
        <el-table-column label="租户 ID" prop="tenantId" min-width="220" />
      </el-table>
    </el-dialog>

    <el-drawer
      v-model="tenantDrawerVisible"
      :title="`管理「${currentTemplate?.template_name}」的使用租户`"
      size="680px"
    >
      <div v-loading="tenantDrawerLoading">
        <el-alert
          v-if="currentTemplate?.template_type === 'SCOPE'"
          type="info"
          :closable="false"
          style="margin-bottom: 16px"
        >
          偏置表示该租户已修改自己的参数，与当前模板不一致。可点击“同步回模板”恢复一致。
        </el-alert>
        <el-table :data="tenantDrawerList" stripe>
          <el-table-column label="租户名" min-width="140">
            <template #default="{ row }">
              {{ row.tenantName || row.tenantId }}
            </template>
          </el-table-column>
          <el-table-column label="Scope ID" min-width="180">
            <template #default="{ row }">
              <template v-if="row.scopeIds && row.scopeIds.length">
                <el-tag
                  v-for="sid in row.scopeIds"
                  :key="sid"
                  type="info"
                  size="small"
                  style="margin-right: 4px"
                >{{ sid }}</el-tag>
              </template>
              <span v-else class="muted">未绑定</span>
            </template>
          </el-table-column>
          <el-table-column label="租户 ID" prop="tenantId" min-width="220" show-overflow-tooltip />
          <el-table-column label="模板版本" prop="templateVersion" width="100" />
          <el-table-column label="当前版本" prop="currentVersion" width="100" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.isDeviated" type="danger" size="small">已偏置</el-tag>
              <el-tag v-else type="success" size="small">一致</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.isDeviated"
                size="small"
                type="warning"
                :loading="row.syncing"
                @click="syncTenant(row)"
              >
                同步
              </el-button>
              <el-button
                size="small"
                type="danger"
                :loading="row.removing"
                @click="removeTenant(row)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { listTemplates, applyTemplate, deleteTemplate } from '@/api/template'
import { getTenantList } from '@/api/tenant'
import { listTenantScopeConfigs, syncTenantFromTemplate, deleteTenantScopeConfig } from '@/api/tenant-scope-config'
import type { Template, TemplateApplyResult, TemplateType, TemplateTenantUsage, TenantScopeConfig, TenantScopeConfigDeleteResult } from '@/types/config'
import type { Tenant } from '@/types/tenant'

const route = useRoute()
const router = useRouter()

const activeSubTab = ref<'builtin' | 'custom'>('builtin')
const list = ref<Template[]>([])
const loading = ref(false)
const applying = ref(false)
const filterType = ref<TemplateType | ''>('')
const tenants = ref<Tenant[]>([])
const currentTemplate = ref<Template | null>(null)
const usageDialogVisible = ref(false)
const usageTenants = ref<TemplateTenantUsage[]>([])
const usageLoading = ref(false)
const tenantDrawerVisible = ref(false)
const tenantDrawerLoading = ref(false)
const tenantDrawerList = ref<Array<TenantScopeConfig & { syncing?: boolean; removing?: boolean }>>([])
const applyDialogVisible = ref(false)
const applyForm = ref<{ targetTenantIds: string[]; reason: string }>({
  targetTenantIds: [],
  reason: '',
})

const hasScopeType = computed(() => list.value.some((item) => item.template_type === 'SCOPE'))

/** 应用模板弹框的可选租户：排除已应用"当前这个模板"的（避免重复应用），应用了其他模板的保留以便覆盖 */
const applicableTenants = computed(() => {
  const cur = currentTemplate.value
  if (!cur) return tenants.value
  return tenants.value.filter((t) => t.currentTemplateId !== cur.id)
})

const loadList = async () => {
  loading.value = true
  try {
    const isBuiltin = activeSubTab.value === 'builtin'
    list.value = await listTemplates(filterType.value || undefined, isBuiltin)
  } catch (e: any) {
    ElMessage.error('加载模板列表失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

const loadTenants = async () => {
  try {
    const result = await getTenantList()
    tenants.value = result.list
  } catch {
    tenants.value = []
  }
}

const onCreate = () => {
  router.push('/config/templates/new')
}

const onDelete = async (row: Template) => {
  try {
    // 先查询该模板绑定的租户，在确认弹框中告知用户哪些 scope 会被清理
    let boundTenants: TenantScopeConfig[] = []
    try {
      boundTenants = await listTenantScopeConfigs(row.id)
    } catch {
      // 查询失败不阻塞删除流程，仅不展示绑定信息
    }
    const tenantInfo = boundTenants.length > 0
      ? `\n\n⚠️ 该模板当前被 ${boundTenants.length} 个租户应用，删除后以下所有租户的自定义配置将被全部清除（内核 scope 配置 + 数据库绑定记录），这些租户将回退到默认配置：\n${boundTenants.map((t) => `  • ${t.tenantName || t.tenantId} (scope: ${t.scopeId || '—'})`).join('\n')}`
      : ''
    await ElMessageBox.confirm(
      `确定要删除模板 "${row.template_name}" 吗？${tenantInfo}`,
      '删除模板',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消', dangerouslyUseHTMLString: false }
    )
    const result = await deleteTemplate(row.id)
    // 根据内核清理结果展示详情
    if (result.kernelFailCount > 0) {
      const failedDetail = result.cleanedScopes
        .filter((s) => !s.kernelDeleted)
        .map((s) => `${s.tenantName || s.tenantId} (scope: ${s.scopeId || '—'}): ${s.errorMessage || '未知错误'}`)
        .join('\n')
      await ElMessageBox.alert(
        `模板已删除，但 ${result.kernelFailCount} 个 scope 的内核配置清理失败（成功 ${result.kernelSuccessCount} 个）。\n\n失败详情:\n${failedDetail}\n\n请手动检查内核服务状态。`,
        '部分清理失败',
        { type: 'warning', confirmButtonText: '知道了' }
      )
    } else if (result.cleanedScopes.length > 0) {
      ElMessage.success(`删除成功，已清理 ${result.kernelSuccessCount} 个 scope 的内核配置`)
    } else {
      ElMessage.success('删除成功')
    }
    loadList()
  } catch (e: any) {
    if (e !== 'cancel') {
      ElMessage.error('删除失败: ' + (e?.message || e))
    }
  }
}

const openDetailPage = (row: Template) => {
  router.push({ path: `/config/templates/${row.id}`, query: { mode: 'view' } })
}

const openEditPage = (row: Template) => {
  router.push({ path: `/config/templates/${row.id}`, query: { mode: 'edit' } })
}

const openCopyPage = (row: Template) => {
  router.push({ path: `/config/templates/${row.id}`, query: { mode: 'copy' } })
}

const onApply = (row: Template) => {
  currentTemplate.value = row
  applyForm.value = { targetTenantIds: [], reason: '' }
  applyDialogVisible.value = true
}

const confirmApply = async () => {
  if (!currentTemplate.value) return
  if (currentTemplate.value.template_type === 'SCOPE' && applyForm.value.targetTenantIds.length === 0) {
    ElMessage.warning('SCOPE 模板必须选择至少一个目标租户')
    return
  }

  if (currentTemplate.value.template_type === 'SCOPE') {
    const overridingTenants = applyForm.value.targetTenantIds
      .map((id) => tenants.value.find((t) => t.id === id))
      .filter((t): t is Tenant =>
        !!t && !!t.currentTemplateId && t.currentTemplateId !== currentTemplate.value!.id
      )

    if (overridingTenants.length > 0) {
      const tenantRows = overridingTenants
        .map((t) => `• ${t.name || t.id}（当前模板：${t.currentTemplateName || t.currentTemplateId}）`)
        .join('\n')
      try {
        await ElMessageBox.confirm(
          `以下租户已应用其他模板，继续应用将覆盖其现有配置：\n\n${tenantRows}\n\n是否确认覆盖？`,
          '覆盖确认',
          { type: 'warning', dangerouslyUseHTMLString: false }
        )
      } catch (e) {
        if (e === 'cancel') return
      }
    }
  }

  if (currentTemplate.value.template_type === 'INSTANCE') {
    try {
      await ElMessageBox.confirm(
        'INSTANCE 模板应用会更新全局参数，修改后需刷新服务才能完全生效。确认应用？',
        '应用 INSTANCE 模板',
        { type: 'warning' }
      )
    } catch (e) {
      if (e === 'cancel') return
    }
  }

  applying.value = true
  try {
    const result: TemplateApplyResult = await applyTemplate(currentTemplate.value.id, {
      targetTenantIds: applyForm.value.targetTenantIds,
      reason: applyForm.value.reason,
    })
    if (result.successCount > 0) {
      let message = `应用成功 ${result.successCount} 个`
      if (result.failCount > 0) message += `，失败 ${result.failCount} 个`
      ElMessage.success(message)
    } else {
      ElMessage.error('应用失败')
    }
    applyDialogVisible.value = false
    // 应用成功后必须同步刷新租户列表，否则 applicableTenants 过滤器
    // 仍拿着旧的 currentTemplateId，会把刚改挂的租户误判为"已应用本模板"而排除
    await Promise.all([loadList(), loadTenants()])
  } catch (e: any) {
    ElMessage.error('应用失败: ' + e.message)
  } finally {
    applying.value = false
  }
}

const openUsageDialog = async (row: Template) => {
  currentTemplate.value = row
  usageDialogVisible.value = true
  usageLoading.value = true
  try {
    usageTenants.value = row.tenant_usage || []
  } catch (e: any) {
    ElMessage.error('加载租户列表失败: ' + e.message)
  } finally {
    usageLoading.value = false
  }
}

const openTenantDrawer = async (row: Template) => {
  currentTemplate.value = row
  tenantDrawerVisible.value = true
  tenantDrawerLoading.value = true
  try {
    if (!row.tenant_usage?.length) {
      tenantDrawerList.value = []
      return
    }
    // 后端按 templateId SQL 过滤（SQL 层已剔除 config_json 大字段），一次请求拿到当前模板的租户列表
    const list = await listTenantScopeConfigs(row.id)
    tenantDrawerList.value = list.map((c) => ({ ...c, syncing: false }))
  } catch (e: any) {
    ElMessage.error('加载租户配置失败: ' + e.message)
    tenantDrawerList.value = []
  } finally {
    tenantDrawerLoading.value = false
  }
}

const syncTenant = async (row: TenantScopeConfig & { syncing?: boolean }) => {
  row.syncing = true
  try {
    await ElMessageBox.confirm(
      `确定将租户「${row.tenantName || row.tenantId}」的配置同步回模板？这会覆盖该租户的自定义参数。`,
      '同步回模板',
      { type: 'warning' }
    )
    await syncTenantFromTemplate(row.tenantId)
    ElMessage.success('同步成功')
    await openTenantDrawer(currentTemplate.value!)
    await loadList()
  } catch (e: any) {
    if (e === 'cancel') return
    ElMessage.error('同步失败: ' + e.message)
  } finally {
    row.syncing = false
  }
}

/**
 * 删除租户的 Scope 配置：清除内核 KV 中的 scope 配置 + DB 绑定记录，
 * 使该租户从当前模板解绑并回退到默认配置。租户本身不删除。
 */
const removeTenant = async (row: TenantScopeConfig & { removing?: boolean }) => {
  const scopeId = row.scopeId || (row.scopeIds && row.scopeIds.length > 0 ? row.scopeIds[0] : '—')
  try {
    await ElMessageBox.confirm(
      `确定要删除租户「${row.tenantName || row.tenantId}」的 Scope 配置吗？\n\n` +
      `⚠️ 此操作将清除该租户的内核 Scope 配置（scope: ${scopeId}）和数据库绑定记录，` +
      `该租户将从当前模板解绑并回退到默认配置。租户本身不会被删除。\n\n` +
      `如需重新应用配置，可再次对该租户应用模板。`,
      '删除 Scope 配置',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户取消
  }
  row.removing = true
  try {
    const result: TenantScopeConfigDeleteResult = await deleteTenantScopeConfig(row.tenantId)
    if (result.kernelDeleted && result.dbBindingDeleted) {
      ElMessage.success(`已删除租户「${row.tenantName || row.tenantId}」的 Scope 配置，该租户已回退到默认配置`)
    } else if (!result.kernelDeleted && !result.dbBindingDeleted) {
      await ElMessageBox.alert(
        `删除失败：内核配置和 DB 绑定记录均未删除。\n\n错误详情: ${result.errorMessage || '未知错误'}\n\n请检查服务状态后重试。`,
        '删除失败',
        { type: 'error', confirmButtonText: '知道了' }
      )
    } else {
      // 部分成功
      const parts: string[] = []
      if (!result.kernelDeleted) parts.push(`内核配置未删除: ${result.errorMessage || '未知错误'}`)
      if (!result.dbBindingDeleted) parts.push(`DB 绑定记录未删除: ${result.errorMessage || '未知错误'}`)
      await ElMessageBox.alert(
        `部分删除失败：\n\n${parts.join('\n')}\n\n请手动检查服务状态。`,
        '部分删除失败',
        { type: 'warning', confirmButtonText: '知道了' }
      )
    }
    // 刷新抽屉列表 + 模板列表（tenant_usage 计数会变）
    // 同时刷新租户列表，否则 applicableTenants 仍拿着旧的 currentTemplateId，
    // 会把刚解绑的租户误判为"已应用本模板"而在应用弹框中排除（无法选中）
    await openTenantDrawer(currentTemplate.value!)
    await Promise.all([loadList(), loadTenants()])
  } catch (e: any) {
    const msg = e?.response?.data?.detail || e?.message || '删除失败'
    if (msg.includes('不存在') || msg.includes('not found') || e?.response?.status === 404) {
      ElMessage.info(`租户「${row.tenantName || row.tenantId}」当前无 Scope 配置，无需删除`)
    } else {
      ElMessage.error(msg)
    }
  } finally {
    row.removing = false
  }
}

watch(activeSubTab, loadList)

onMounted(() => {
  // 从路由参数读取标签页，默认显示"自定义"
  const tab = route.query.tab as string
  if (tab === 'custom' || tab === 'builtin') {
    activeSubTab.value = tab
  }
  
  loadList()
  loadTenants()
})
</script>

<style scoped>
.template-list {
  padding: 0;
}

.sub-tabs {
  margin-top: 0;
}

.sub-tabs :deep(.el-tabs__content) {
  padding-top: 16px;
}

.toolbar {
  margin-top: 8px;
  margin-bottom: 16px;
}

.desc {
  font-size: 12px;
  color: #666;
}

.muted {
  color: #999;
}

.hint {
  font-size: 11px;
  color: #999;
  margin-top: 4px;
}
</style>
