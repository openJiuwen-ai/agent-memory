<template>
  <div class="template-detail-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <div>
            <el-button type="default" @click="goBack" style="margin-bottom: 12px">
              <el-icon><ArrowLeft /></el-icon>
              返回模板列表
            </el-button>
            <h2>
              {{ pageTitle }}
              <el-tag v-if="template && template.status === 'draft'" type="warning" style="margin-left: 12px">
                草稿未发布
              </el-tag>
              <el-tag v-else-if="template" type="success" style="margin-left: 12px">
                已发布
              </el-tag>
            </h2>
            <div class="sub-title">
              <template v-if="template && template.status === 'draft'">
                <span style="color: #e6a23c">⚠️ 当前为草稿状态，配置尚未推送到内核</span>
                <span v-if="isViewMode"> | 点击"编辑"修改后请点击"保存修改"发布</span>
              </template>
              <template v-else>
                {{ pageHint }}
              </template>
            </div>
          </div>
          <div class="header-actions" v-if="isViewMode && template">
            <el-button type="primary" @click="switchToEdit">编辑</el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="loading" :rows="8" animated />

      <template v-else>
        <el-descriptions v-if="template && !isCreateMode" :column="4" border style="margin-bottom: 16px">
          <el-descriptions-item label="模板名">{{ form.template_name }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ form.template_type }}</el-descriptions-item>
          <el-descriptions-item label="版本">v{{ template.version || 1 }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <div v-if="template.status === 'draft'">
              <el-tag type="warning">草稿</el-tag>
              <div style="font-size: 12px; color: #909399; margin-top: 4px">点击"保存修改"推送到内核</div>
            </div>
            <div v-else>
              <el-tag type="success">已发布</el-tag>
              <div style="font-size: 12px; color: #909399; margin-top: 4px">配置已同步到内核</div>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="使用租户数">
            {{ usageTenants.length }}
          </el-descriptions-item>
          <el-descriptions-item label="说明" :span="4">
            {{ form.description || '—' }}
          </el-descriptions-item>
        </el-descriptions>

        <el-form :model="form" label-width="120px">
          <el-form-item label="来源模板" v-if="isCreateMode" required>
            <el-select
              v-model="selectedSourceTemplateId"
              placeholder="请选择现有模板作为来源"
              style="width: 100%"
              @change="onSourceTemplateChange"
            >
              <el-option
                v-for="t in sourceTemplates"
                :key="t.id"
                :label="`${t.template_name} (${t.template_type})`"
                :value="t.id"
              />
            </el-select>
            <div class="hint">选择来源后，会自动复制其配置项，可在此基础上修改</div>
          </el-form-item>

          <el-form-item label="模板名称" required>
            <el-input
              v-model="form.template_name"
              :disabled="!isEditableName"
              placeholder="字母数字下划线，唯一"
            />
          </el-form-item>

          <el-form-item label="模板类型" required>
            <el-select
              v-model="form.template_type"
              :disabled="!isEditableName"
              style="width: 100%"
              @change="onTypeChange"
            >
              <el-option label="SCOPE — 租户级（应用到租户）" value="SCOPE" />
              <el-option label="INSTANCE — 实例级（单例）" value="INSTANCE" />
            </el-select>
          </el-form-item>

          <el-form-item label="说明">
            <el-input
              v-model="form.description"
              type="textarea"
              :rows="2"
              :disabled="isViewMode"
            />
          </el-form-item>

          <el-form-item label="使用租户" v-if="form.template_type === 'SCOPE' && usageTenants.length > 0">
            <div class="tenant-box">
              <el-tag
                v-for="tenant in usageTenants.slice(0, 8)"
                :key="tenant.tenantId"
                size="small"
                style="margin-right: 6px; margin-bottom: 6px"
              >
                {{ tenant.tenantName || tenant.tenantId }}
              </el-tag>
              <el-button
                v-if="usageTenants.length > 8"
                link
                type="primary"
                @click="usageDialogVisible = true"
              >
                过多（{{ usageTenants.length }}）
              </el-button>
            </div>
          </el-form-item>

          <el-form-item label="配置项" required v-if="!isCreateMode || selectedSourceTemplateId">
            <div class="config-editor">
              <el-table :data="configItems" border>
                <el-table-column label="配置项" min-width="280">
                  <template #default="{ row }">
                    <el-input
                      v-model="row.key"
                      :disabled="isViewMode"
                      placeholder="如 model_cfg.model"
                    />
                  </template>
                </el-table-column>
                <el-table-column label="值" min-width="380">
                  <template #default="{ row }">
                    <el-input
                      v-model="row.value"
                      :disabled="isViewMode"
                      placeholder="请输入值"
                    />
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="90" v-if="!isViewMode">
                  <template #default="{ $index }">
                    <el-button link type="danger" @click="removeConfigItem($index)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <div class="config-toolbar" v-if="!isViewMode && (!isCreateMode || selectedSourceTemplateId)">
                <el-button @click="addConfigItem">新增配置项</el-button>
              </div>
            </div>
          </el-form-item>

          <el-form-item label="应用目标租户" v-if="showTargetTenants">
            <el-select
              v-model="form.target_tenant_ids"
              multiple
              filterable
              style="width: 100%"
              placeholder="选择要应用的租户（不选则只保存模板）"
            >
              <el-option v-for="t in tenants" :key="t.id" :label="t.name" :value="t.id" />
            </el-select>
            <div class="hint" v-if="isCreateOrCopyMode">
              新建/复制时选择目标租户并点击"应用"即可绑定；编辑模式不提供修改绑定，请使用模板列表的"应用"按钮增加租户，或在租户管理页"清除Scope配置"移除租户。
            </div>
          </el-form-item>

          <el-form-item label="变更原因" v-if="!isViewMode">
            <el-input v-model="form.reason" placeholder="可选" />
          </el-form-item>
        </el-form>

        <div class="page-actions" v-if="!isViewMode">
          <el-button @click="goBack">取消</el-button>
          <!-- 新建/复制模式：走 saveCreate → createTemplate/copyTemplate 创建新模板 -->
          <el-button v-if="isCreateOrCopyMode" @click="saveCreate('only')" :loading="saving">确定（仅创建）</el-button>
          <el-button
            v-if="isCreateOrCopyMode"
            type="primary"
            @click="saveCreate('apply')"
            :loading="saving"
          >
            {{ form.template_type === 'INSTANCE' ? '保存（自动应用单例）' : '应用（需选租户）' }}
          </el-button>
          <!-- 编辑模式：走 saveUpdate → updateTemplate 修改现有模板 -->
          <el-button v-if="!isCreateOrCopyMode" @click="saveUpdate(false)" :loading="saving">
            保存草稿
          </el-button>
          <el-button v-if="!isCreateOrCopyMode" type="primary" @click="saveUpdate(true)" :loading="saving">
            保存修改
          </el-button>
          <div v-if="!isCreateOrCopyMode" class="action-hint">
            <span v-if="template?.status === 'draft'">
              💡 提示：当前为草稿状态，修改后请点击"保存修改"推送到内核
            </span>
            <span v-else>
              💡 提示："保存草稿"仅保存不推送，"保存修改"保存并推送到内核
            </span>
          </div>
        </div>
      </template>
    </el-card>

    <el-dialog
      v-model="usageDialogVisible"
      :title="`使用「${form.template_name}」的租户`"
      width="520px"
    >
      <el-table :data="usageTenants">
        <el-table-column label="租户名" prop="tenantName" min-width="180" />
        <el-table-column label="租户 ID" prop="tenantId" min-width="220" />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { createTemplate, copyTemplate, getTemplate, listTemplates, updateTemplate } from '@/api/template'
import { getTenantList } from '@/api/tenant'
import { listTenantScopeConfigs } from '@/api/tenant-scope-config'
import type { Template, TemplateApplyResult, TemplateType, TenantScopeConfig } from '@/types/config'
import type { Tenant } from '@/types/tenant'

type ConfigItem = {
  key: string
  value: string
}

const defaultScopeConfigItems = (): ConfigItem[] => [
  { key: 'model_cfg.model', value: 'qwen-plus' },
  { key: 'model_cfg.temperature', value: '0.1' },
  { key: 'model_cfg.max_tokens', value: '2000' },
  { key: 'model_client_cfg.client_provider', value: '' },
  { key: 'model_client_cfg.api_base', value: '' },
  { key: 'model_client_cfg.api_key', value: '' },
  { key: 'model_client_cfg.verify_ssl', value: 'false' },
  { key: 'model_client_cfg.timeout', value: '90.0' },
  { key: 'embedding_cfg.model_name', value: 'BAAI/bge-m3' },
  { key: 'embedding_cfg.base_url', value: '' },
  { key: 'embedding_cfg.api_key', value: '' },
  { key: 'user_profile_definition', value: '用户本人的肯定或否定表述（包含不限于基本身份、兴趣偏好、人际关系、资产状况）' },
  { key: 'semantic_memory_definition', value: '用户对话中涉及的和时间无明确关系的事实性内容或概念' },
  { key: 'episodic_memory_definition', value: '用户对话中涉及的和时间有明确关系的事实性内容或概念' },
  { key: 'extract_assistant_memory', value: 'false' },
  { key: 'use_query_rewrite', value: 'false' },
  { key: 'use_when_to_use', value: 'false' },
]

const defaultInstanceConfigItems = (): ConfigItem[] => [
  { key: 'MODEL_PROVIDER', value: '' },
  { key: 'API_BASE', value: '' },
  { key: 'API_KEY', value: '' },
  { key: 'MODEL_NAME', value: 'qwen-plus' },
  { key: 'EMBED_MODEL_NAME', value: 'BAAI/bge-m3' },
  { key: 'EMBED_API_BASE', value: '' },
  { key: 'EMBED_API_KEY', value: '' },
  { key: 'MEMORY_INDEX_TYPE', value: 'vector' },
  { key: 'RERANK_API_BASE', value: '' },
  { key: 'RERANK_API_KEY', value: '' },
  { key: 'RERANK_MODEL_NAME', value: 'BAAI/bge-reranker-v2' },
  { key: 'RERANK_THRESHOLD', value: '0.3' },
  { key: 'RERANK_POOL_FACTOR', value: '3' },
  { key: 'DB_URL', value: '' },
  { key: 'KV_SHELVE_PATH', value: '' },
  { key: 'VECTOR_CHROMA_PERSIST_DIR', value: '' },
  { key: 'MEMORY_ENABLE_MIDDLE_MEMORY', value: 'true' },
  { key: 'MEMORY_MIDDLE_CHECK_INTERVAL', value: '50' },
  { key: 'MEMORY_ENABLE_FORGETTING', value: 'false' },
  { key: 'MEMORY_FORGET_INTERVAL', value: '86400' },
  { key: 'MEMORY_FORGET_LAMBDA', value: '0.1' },
  { key: 'MEMORY_FORGET_THRESHOLD', value: '0.5' },
  { key: 'MEMORY_FORGET_COOLDOWN', value: '3600' },
  { key: 'MEMORY_FORGET_DEFAULT_IMPORTANCE', value: '5' },
  { key: 'MEMORY_FORGET_EXEMPT_IMPORTANCE', value: '8' },
]

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const saving = ref(false)
const usageDialogVisible = ref(false)
const template = ref<Template | null>(null)
const tenants = ref<Tenant[]>([])
const usageTenants = ref<TenantScopeConfig[]>([])
const sourceTemplates = ref<Template[]>([])
const selectedSourceTemplateId = ref<string>('')

const form = ref<{
  template_name: string
  description: string
  template_type: TemplateType
  target_tenant_ids: string[]
  reason: string
}>({
  template_name: '',
  description: '',
  template_type: 'SCOPE',
  target_tenant_ids: [],
  reason: '',
})
const configItems = ref<ConfigItem[]>([])

const mode = computed(() => String(route.query.mode || (route.params.id ? 'view' : 'create')))
const isCreateMode = computed(() => mode.value === 'create')
const isCopyMode = computed(() => mode.value === 'copy')
const isViewMode = computed(() => mode.value === 'view')
// 复制模式与新建模式一样都是创建新模板，保存逻辑走 saveCreate → copyTemplate/createTemplate，
// 而非 saveUpdate → updateTemplate（后者会修改源模板）。
const isCreateOrCopyMode = computed(() => isCreateMode.value || isCopyMode.value)
const isEditableName = computed(() => isCreateMode.value || isCopyMode.value)
// 仅新建/复制模式显示"应用目标租户"选择框；编辑模式不提供修改绑定（编辑只改配置）。
// 增加租户走模板列表"应用"按钮，移除租户走租户管理页"清除Scope配置"。
const showTargetTenants = computed(() => isCreateOrCopyMode.value && form.value.template_type === 'SCOPE')
const templateId = computed(() => String(route.params.id || ''))

const pageTitle = computed(() => {
  if (isCreateMode.value) return '新建模板'
  if (isCopyMode.value) return '复制模板'
  if (mode.value === 'edit') return '编辑模板'
  return '模板详情'
})

const pageHint = computed(() => {
  if (isViewMode.value) return '查看模板完整配置项、使用租户和版本信息'
  if (form.value.template_type === 'INSTANCE') return 'INSTANCE 模板保存后会作用于全局参数'
  return 'SCOPE 模板可直接应用到指定租户并热生效'
})

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  Object.prototype.toString.call(value) === '[object Object]'

const serializeValue = (value: unknown): string => {
  if (value === null) return 'null'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const flattenConfig = (value: unknown, prefix = ''): ConfigItem[] => {
  if (isPlainObject(value)) {
    return Object.entries(value).flatMap(([key, child]) =>
      flattenConfig(child, prefix ? `${prefix}.${key}` : key)
    )
  }
  if (!prefix) return []
  return [{ key: prefix, value: serializeValue(value) }]
}

const parseConfigItems = (configJson: string): ConfigItem[] => {
  try {
    const parsed = JSON.parse(configJson)
    const items = flattenConfig(parsed)
    return items.length > 0 ? items : [{ key: '', value: '' }]
  } catch {
    return [{ key: '', value: '' }]
  }
}

const parseLooseValue = (text: string): unknown => {
  const trimmed = text.trim()
  if (trimmed === 'true') return true
  if (trimmed === 'false') return false
  if (trimmed === 'null') return null
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed)
  if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
    try {
      return JSON.parse(trimmed)
    } catch {
      return text
    }
  }
  return text
}

const setNestedValue = (target: Record<string, unknown>, path: string, value: unknown) => {
  const keys = path.split('.').map((item) => item.trim()).filter(Boolean)
  if (keys.length === 0) return
  let current: Record<string, unknown> = target
  keys.forEach((key, index) => {
    if (index === keys.length - 1) {
      current[key] = value
      return
    }
    if (!isPlainObject(current[key])) current[key] = {}
    current = current[key] as Record<string, unknown>
  })
}

const buildConfigJson = (items: ConfigItem[]) => {
  const result: Record<string, unknown> = {}
  items.forEach((item) => {
    if (!item.key.trim()) return
    setNestedValue(result, item.key, parseLooseValue(item.value))
  })
  return JSON.stringify(result)
}

const addConfigItem = () => {
  configItems.value.push({ key: '', value: '' })
}

const removeConfigItem = (index: number) => {
  configItems.value.splice(index, 1)
  if (configItems.value.length === 0) configItems.value.push({ key: '', value: '' })
}

const loadTenants = async () => {
  try {
    const r = await getTenantList()
    tenants.value = r.list
  } catch {
    tenants.value = []
  }
}

const loadSourceTemplates = async () => {
  if (!isCreateMode.value) return
  try {
    sourceTemplates.value = await listTemplates()
  } catch {
    sourceTemplates.value = []
  }
}

const onSourceTemplateChange = async (sourceId: string) => {
  if (!sourceId) {
    configItems.value = []
    return
  }
  try {
    const source = await getTemplate(sourceId)
    form.value.template_type = source.template_type
    form.value.description = source.description || ''
    configItems.value = parseConfigItems(source.config_json)
  } catch (e: any) {
    ElMessage.error('加载来源模板失败: ' + e.message)
  }
}

const loadUsageTenants = async (id: string) => {
  try {
    // 后端按 templateId SQL 过滤（templateId 必填），直接拿到绑定该模板的租户列表
    const list = await listTenantScopeConfigs(id)
    usageTenants.value = Array.isArray(list) ? list : []
    // 编辑模式回显：把已绑定的租户 id 填入"应用目标租户"选择框，
    // 这样用户进入编辑页能看到当前已绑定的租户，保存时也会一并提交。
    form.value.target_tenant_ids = usageTenants.value.map((t) => t.tenantId)
  } catch {
    usageTenants.value = []
    form.value.target_tenant_ids = []
  }
}

const loadPage = async () => {
  loading.value = true
  try {
    await loadTenants()
    await loadSourceTemplates()
    if (isCreateMode.value) {
      template.value = null
      form.value = {
        template_name: '',
        description: '',
        template_type: 'SCOPE',
        target_tenant_ids: [],
        reason: '',
      }
      configItems.value = defaultScopeConfigItems()
      usageTenants.value = []
      return
    }

    const t = await getTemplate(templateId.value)
    template.value = t
    form.value = {
      template_name: isCopyMode.value ? `${t.template_name}_copy` : t.template_name,
      description: t.description || '',
      template_type: t.template_type,
      target_tenant_ids: [],
      reason: '',
    }
    configItems.value = parseConfigItems(t.config_json)
    if (t.template_type === 'SCOPE') {
      await loadUsageTenants(t.id)
      // 复制模式：loadUsageTenants 会把源模板的租户回填到 target_tenant_ids，
      // 但复制是创建新模板，不应继承源模板的租户绑定，需清空让用户重新选择。
      if (isCopyMode.value) {
        form.value.target_tenant_ids = []
      }
    } else {
      usageTenants.value = []
    }
  } catch (e: any) {
    ElMessage.error('加载模板失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

const onTypeChange = () => {
  if (!isCreateMode.value) return
  configItems.value = form.value.template_type === 'INSTANCE'
    ? defaultInstanceConfigItems()
    : defaultScopeConfigItems()
}

const goBack = () => {
  router.push({ path: '/config', query: { tab: 'custom' } })
}

const switchToEdit = () => {
  router.replace({ path: `/config/templates/${templateId.value}`, query: { mode: 'edit' } })
}

const saveCreate = async (saveMode: 'only' | 'apply') => {
  // 新建模式必须选择来源模板；复制模式来源即当前 templateId（已加载配置），无需选择
  if (isCreateMode.value && !selectedSourceTemplateId.value) {
    ElMessage.warning('请选择来源模板')
    return
  }
  if (!form.value.template_name.trim()) {
    ElMessage.warning('请输入模板名称')
    return
  }
  const targetIds = saveMode === 'apply' && form.value.template_type === 'SCOPE'
    ? form.value.target_tenant_ids
    : []
  if (saveMode === 'apply' && form.value.template_type === 'SCOPE' && targetIds.length === 0) {
    ElMessage.warning('请选择至少一个目标租户')
    return
  }
  saving.value = true
  try {
    const payload = {
      template_name: form.value.template_name,
      description: form.value.description,
      template_type: form.value.template_type,
      config_json: buildConfigJson(configItems.value),
      target_tenant_ids: targetIds,
      reason: form.value.reason,
    }
    const result: TemplateApplyResult = isCopyMode.value
      ? await copyTemplate(templateId.value, payload)
      : await createTemplate(payload)
    // 根据下发结果区分提示：全部成功 / 部分失败 / 全部失败（全部失败后端已抛异常走 catch）
    if (result.failCount > 0 && result.successCount > 0) {
      // 部分成功：模板已保存，但部分租户下发内核失败，需告知用户具体失败项
      const failedDetail = result.results
        .filter((r) => !r.success)
        .map((r) => `${r.tenantName || r.tenantId}: ${r.errorMessage || '未知错误'}`)
        .join('\n')
      await ElMessageBox.alert(
        `模板已保存，但 ${result.failCount} 个租户配置下发内核失败（成功 ${result.successCount} 个）。\n\n失败详情:\n${failedDetail}\n\n请检查内核服务状态后重试。`,
        '部分下发失败',
        { type: 'warning', confirmButtonText: '知道了' }
      )
    } else if (result.successCount > 0) {
      ElMessage.success(`已保存并应用 ${result.successCount} 个目标`)
    } else {
      // 无目标租户的纯保存（INSTANCE 自动应用或 SCOPE 仅创建）
      ElMessage.success('模板已保存')
    }
    goBack()
  } catch (e: any) {
    // 400 业务校验错误用弹框展示（拦截器已把 message 挂到 e.message），其他错误回退 toast
    if (e?.message) {
      await ElMessageBox.alert(e.message, '保存失败', { type: 'error', confirmButtonText: '知道了' })
    }
  } finally {
    saving.value = false
  }
}

const saveUpdate = async (apply: boolean = true) => {
  saving.value = true
  try {
    await updateTemplate(templateId.value, {
      description: form.value.description,
      config_json: buildConfigJson(configItems.value),
      reason: form.value.reason,
      apply: apply,
      // 编辑模式不再管理租户绑定：不传 targetTenantIds，后端仅对已绑定租户重下发配置。
      // 增加租户走模板列表"应用"按钮，移除租户走租户管理页"清除Scope配置"。
      targetTenantIds: undefined,
    })
    ElMessage.success(apply ? '模板已更新并应用' : '草稿已保存')
    router.replace({ path: `/config/templates/${templateId.value}`, query: { mode: 'view' } })
    await loadPage()
  } catch (e: any) {
    // 400 业务校验错误用弹框展示（拦截器已把 message 挂到 e.message），其他错误回退 toast
    if (e?.message) {
      await ElMessageBox.alert(e.message, '保存失败', { type: 'error', confirmButtonText: '知道了' })
    }
  } finally {
    saving.value = false
  }
}

onMounted(loadPage)
watch(() => route.fullPath, loadPage)
</script>

<style scoped>
.template-detail-page {
  padding: 16px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}
.page-header h2 {
  margin: 4px 0 6px;
}
.sub-title {
  color: #666;
  font-size: 13px;
}
.tenant-box,
.config-editor {
  width: 100%;
}
.config-toolbar {
  margin-top: 12px;
}
.page-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 24px;
  flex-wrap: wrap;
}
.action-hint {
  width: 100%;
  text-align: right;
  margin-top: 8px;
  font-size: 13px;
  color: #909399;
}
</style>
