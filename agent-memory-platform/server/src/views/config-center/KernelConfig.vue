<!--
  内核配置 Tab — 2026-07-19 P0-3 v3 重构
  只读展示全局参数，不可修改：
  - 安装参数：部署时确定（IP/PORT/数据目录/存储类型/密钥）
  - 连接参数：LLM/Embedding 连接的全局默认值，scope 未配置时兜底

  可修改参数请到「配置模板」Tab：
  - 热启动模板（tpl_instance_hot）：修改后立即生效
  - 冷启动模板（tpl_instance_cold）：修改后需重启引擎生效
-->
<template>
  <div class="kernel-config">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>内核配置 — 只读</span>
          <div>
            <el-tag size="small" :type="configAvailable ? 'success' : 'info'">
              {{ configAvailable ? '已连接' : '不可用' }}
            </el-tag>
            <el-button size="small" style="margin-left: 8px" @click="loadKernel" :loading="loading">
              刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        style="margin-bottom: 12px"
      >
        内核配置仅展示全局参数（安装参数 + 连接参数），不可修改。可修改参数请到「配置模板」Tab 编辑热启动/冷启动模板。
      </el-alert>

      <el-alert
        v-if="!configAvailable"
        type="warning"
        :closable="false"
        style="margin-bottom: 12px"
      >
        {{ configError || '当前内核未连接，配置查看功能不可用' }}
      </el-alert>

      <el-form v-else label-width="220px">
        <!-- 安装参数 -->
        <el-divider content-position="left">安装参数（部署时确定，不可修改）</el-divider>
        <el-form-item
          v-for="item in installParams"
          :key="item.key"
          :label="item.key"
        >
          <span class="readonly-value">{{ formatValue(item.value) }}</span>
          <el-tag size="small" type="danger" style="margin-left: 6px">安装参数</el-tag>
        </el-form-item>

        <!-- 连接参数 -->
        <el-divider content-position="left">连接参数（全局默认值，scope 未配置时兜底）</el-divider>
        <el-form-item
          v-for="item in connectionParams"
          :key="item.key"
          :label="item.key"
        >
          <span class="readonly-value">{{ formatValue(item.value) }}</span>
          <el-tag size="small" type="warning" style="margin-left: 6px">连接参数</el-tag>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getKernelConfig } from '@/api/config'
import type { KernelConfig } from '@/types/config'

/** 安装参数列表：label 用于展示，key 对齐内核 /admin/config 返回字段名 */
const INSTALL_PARAMS = [
  { label: 'IP', key: 'ip' },
  { label: 'PORT', key: 'port' },
  { label: 'MEMORY_DATA_DIR', key: 'memory_data_dir' },
  { label: 'MEMORY_API_KEY', key: 'memory_api_key' },
  { label: 'KV_STORE_TYPE', key: 'kv_store_type' },
  { label: 'DB_STORE_TYPE', key: 'db_store_type' },
  { label: 'VECTOR_STORE_TYPE', key: 'vector_store_type' },
]

/** 连接参数列表（全局默认值，scope 可覆盖） */
const CONNECTION_PARAMS = [
  { label: 'MODEL_PROVIDER', key: 'model_provider' },
  { label: 'API_BASE', key: 'api_base' },
  { label: 'API_KEY', key: 'api_key' },
  { label: 'MODEL_NAME', key: 'model_name' },
  { label: 'EMBED_MODEL_NAME', key: 'embed_model_name' },
  { label: 'EMBED_API_BASE', key: 'embed_api_base' },
  { label: 'EMBED_API_KEY', key: 'embed_api_key' },
]

const config = ref<KernelConfig>({
  runtime: {},
  storage: {},
  vector_engine: {},
  engine: {},
  restart_required: false,
  source: '',
  available: false,
})
const configAvailable = ref(false)
const configError = ref('')
const loading = ref(false)

/** 从内核返回的分类配置中提取指定 key 的值 */
function extractParam(items: Array<{ label: string; key: string }>): Array<{ key: string; value: any }> {
  const allParams: Record<string, any> = {
    ...config.value.runtime,
    ...config.value.storage,
    ...config.value.vector_engine,
    ...config.value.engine,
  }
  return items.map((item) => {
    const raw = allParams[item.key]
    // 内核返回两种结构：
    //   普通参数: { value: ..., configured, editable, category, danger }
    //   敏感参数: { configured: bool }  — 不含 value 字段（脱敏）
    if (raw && typeof raw === 'object') {
      if ('value' in raw) return { key: item.label, value: raw.value }
      // 敏感参数无 value 字段：按 configured 显示脱敏状态
      if ('configured' in raw) {
        return { key: item.label, value: raw.configured ? '••••••（已配置）' : '未配置' }
      }
    }
    return { key: item.label, value: raw }
  })
}

const installParams = computed(() => extractParam(INSTALL_PARAMS))
const connectionParams = computed(() => extractParam(CONNECTION_PARAMS))

const formatValue = (v: any) => {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

const loadKernel = async () => {
  loading.value = true
  try {
    const k = await getKernelConfig()
    config.value = k
    configAvailable.value = k.available !== false && !k.error
    configError.value = k.error || ''
  } catch (e: any) {
    ElMessage.error('加载内核配置失败: ' + e.message)
    configAvailable.value = false
    configError.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadKernel()
})
</script>

<style scoped>
.kernel-config {
  padding: 0;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.readonly-value {
  color: #666;
  font-family: monospace;
  font-size: 12px;
}
</style>
