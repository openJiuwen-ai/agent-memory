<template>
  <div class="log-center">
    <el-tabs v-model="activeTab" type="border-card">
      <!-- ============ 1. 运行日志（内核，不入库 §6.3.2） ============ -->
      <el-tab-pane label="运行日志" name="runtime">
        <div class="level-selector">
          <el-radio-group v-model="rtSource" size="default" style="margin-right: 16px">
            <el-radio-button label="kernel">内核</el-radio-button>
            <el-radio-button label="platform">服务层</el-radio-button>
          </el-radio-group>
          <el-radio-group v-model="rtAction" size="default">
            <el-radio-button label="query">查询</el-radio-button>
            <el-radio-button label="download">下载</el-radio-button>
          </el-radio-group>
        </div>
        <el-divider />

        <!-- 查询 -->
        <div v-if="rtAction === 'query'">
          <el-alert :title="rtSource === 'kernel' ? '内核运行日志不入库（§6.3.2），通过内核 HTTP 接口瞬时查询' : '服务层自身应用日志（platform.log），按行数瞬时读取'" type="info" :closable="false" show-icon style="margin-bottom: 16px" />
          <div class="filter-bar">
            <el-input-number v-model="rtFilter.lines" :min="1" :max="2000" :step="100" style="width: 140px" />
            <el-select v-model="rtFilter.level" placeholder="级别" clearable style="width: 120px">
              <el-option label="全部" value="" />
              <el-option label="DEBUG" value="DEBUG" />
              <el-option label="INFO" value="INFO" />
              <el-option label="WARNING" value="WARNING" />
              <el-option label="ERROR" value="ERROR" />
              <el-option label="CRITICAL" value="CRITICAL" />
            </el-select>
            <el-input v-model="rtFilter.event_type" placeholder="事件类型（可空）" clearable style="width: 200px" />
            <el-button type="primary" :loading="rtLoading" @click="fetchRuntimeTail">查询</el-button>
          </div>
          <el-input v-model="rtLinesText" type="textarea" :rows="14" readonly placeholder="最近 N 行日志" style="font-family: 'Consolas', monospace; font-size: 12px" />
        </div>

        <!-- 下载 -->
        <div v-if="rtAction === 'download'">
          <el-alert :title="rtSource === 'kernel' ? '先查询有哪些内核日志文件，每个文件后跟下载按钮' : '先查询服务层日志目录下的文件（platform.log 及轮转、access log），每个文件后跟下载按钮'" type="info" :closable="false" show-icon style="margin-bottom: 16px" />
          <div class="filter-bar">
            <el-date-picker v-model="rtFileRange" type="daterange" range-separator="至" start-placeholder="开始日期" end-placeholder="结束日期" format="YYYY-MM-DD" value-format="YYYY-MM-DD" style="width: 280px" />
            <el-button type="primary" :loading="rtFileLoading" @click="fetchRuntimeFiles">查询文件列表</el-button>
          </div>
          <el-table :data="runtimeFiles" border size="small" empty-text="点击查询文件列表">
            <el-table-column prop="filename" label="文件名" min-width="220" show-overflow-tooltip />
            <el-table-column prop="log_type" label="类型" width="120" />
            <el-table-column prop="size_human" label="大小" width="100" />
            <el-table-column prop="created_at" label="创建时间" width="180" show-overflow-tooltip />
            <el-table-column prop="modified_at" label="修改时间" width="180" show-overflow-tooltip />
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" :loading="row._downloading" @click="handleDownloadOneRuntime(row)">下载</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ============ 2. 操作日志 ============ -->
      <el-tab-pane label="操作日志" name="operation">
        <div class="filter-bar">
          <el-date-picker v-model="opFilter.dateRange" type="datetimerange" range-separator="至" start-placeholder="开始" end-placeholder="结束" format="YYYY-MM-DDTHH:mm:ss" value-format="YYYY-MM-DDTHH:mm:ss[Z]" style="width: 380px" />
          <el-input v-model="opFilter.operator_id" placeholder="操作人ID" clearable style="width: 140px" />
          <el-select v-model="opFilter.type" multiple collapse-tags collapse-tags-tooltip :max-collapse-tags="1" placeholder="操作类型(可多选)" clearable style="width: 210px">
            <el-option v-for="t in OP_TYPE_OPTIONS" :key="t.value" :label="t.label" :value="t.value" />
            <template #footer>
              <el-button size="small" link @click="opFilter.type = OP_TYPE_OPTIONS.map(t => t.value)">全选</el-button>
              <el-button size="small" link @click="opFilter.type = []">清空</el-button>
            </template>
          </el-select>
          <el-switch v-model="opFilter.success_only" active-text="仅成功" />
          <el-button type="primary" @click="fetchOpLogs">查询</el-button>
          <el-button @click="handleExportOp">导出 CSV</el-button>
        </div>
        <el-table :data="opLogs" border size="small">
          <el-table-column prop="operated_at" label="时间" width="180" />
          <el-table-column prop="operator_id" label="操作人" width="110" />
          <el-table-column prop="operation_type" label="操作类型" width="130" />
          <el-table-column prop="request_method" label="方法" width="70" />
          <el-table-column prop="request_path" label="路径" min-width="180" show-overflow-tooltip />
          <el-table-column label="状态" width="70">
            <template #default="{ row }"><el-tag :type="(row.response_status ?? 0) < 400 ? 'success' : 'danger'" size="small">{{ row.response_status }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="duration_ms" label="耗时(ms)" width="90" />
        </el-table>
        <el-pagination v-model:current-page="opFilter.page" v-model:page-size="opFilter.size" :total="opTotal" :page-sizes="[10,20,50]" layout="total, sizes, prev, pager, next" style="margin-top: 10px" @current-change="fetchOpLogs" @size-change="fetchOpLogs" />
      </el-tab-pane>

      <!-- ============ 3. 消息日志 ============ -->
      <el-tab-pane label="消息日志" name="message">
        <div class="filter-bar">
          <el-date-picker v-model="msgFilter.dateRange" type="datetimerange" range-separator="至" start-placeholder="开始" end-placeholder="结束" format="YYYY-MM-DDTHH:mm:ss" value-format="YYYY-MM-DDTHH:mm:ss[Z]" style="width: 380px" />
          <el-input v-model="msgFilter.user_id" placeholder="用户ID" clearable style="width: 140px" />
          <el-input v-model="msgFilter.scope_id" placeholder="Scope" clearable style="width: 140px" />
          <el-switch v-model="msgFilter.success_only" active-text="仅成功" />
          <el-button type="primary" @click="fetchMsgLogs">查询</el-button>
          <el-button @click="handleExportMsg">导出 CSV</el-button>
        </div>
        <el-table :data="msgLogs" border size="small">
          <el-table-column prop="created_at" label="时间" width="180" />
          <el-table-column prop="request_id" label="Request ID" width="170" show-overflow-tooltip />
          <el-table-column prop="user_id" label="用户" width="110" />
          <el-table-column prop="scope_id" label="Scope" width="130" />
          <el-table-column prop="api_path" label="API路径" min-width="180" show-overflow-tooltip />
          <el-table-column prop="message_count" label="消息数" width="80" />
        </el-table>
        <el-pagination v-model:current-page="msgFilter.page" v-model:page-size="msgFilter.size" :total="msgTotal" :page-sizes="[10,20,50]" layout="total, sizes, prev, pager, next" style="margin-top: 10px" @current-change="fetchMsgLogs" @size-change="fetchMsgLogs" />
      </el-tab-pane>

      <!-- ============ 4. 一键采集 ============ -->
      <el-tab-pane label="一键采集" name="collect">
        <!-- 采集表单 -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header><span style="font-weight: 600">新建采集</span></template>
          <el-form label-width="100px" inline>
            <el-form-item label="采集场景">
              <el-select v-model="collectForm.scene" placeholder="选择场景" style="width: 180px">
                <el-option label="故障排查" value="故障排查" />
                <el-option label="日常巡检" value="日常巡检" />
                <el-option label="性能诊断" value="性能诊断" />
                <el-option label="上线检查" value="上线检查" />
              </el-select>
            </el-form-item>
            <el-form-item label="时间范围">
              <el-date-picker v-model="collectForm.range" type="daterange" range-separator="至" start-placeholder="开始日期" end-placeholder="结束日期" format="YYYY-MM-DD" value-format="YYYY-MM-DD" style="width: 280px" />
            </el-form-item>
            <el-form-item label="租户ID">
              <el-input v-model="collectForm.tenant" placeholder="可选，默认 default" style="width: 180px" />
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="collectForm.remark" placeholder="可选" style="width: 220px" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="collecting" @click="handleCollect">
                <el-icon><Download /></el-icon>&nbsp;采集
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- 采集记录列表 -->
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">采集记录</span>
              <el-button size="small" @click="fetchCollectRecords">刷新</el-button>
            </div>
          </template>
          <el-table :data="collectRecords" border size="small" empty-text="暂无采集记录">
            <el-table-column prop="name" label="名称" min-width="240" show-overflow-tooltip />
            <el-table-column prop="scene" label="场景" width="110" />
            <el-table-column label="时间范围" width="200">
              <template #default="{ row }">{{ row.start_date }} 至 {{ row.end_date }}</template>
            </el-table-column>
            <el-table-column label="大小" width="100">
              <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
            </el-table-column>
            <el-table-column prop="created_at" label="生成时间" width="180" />
            <el-table-column prop="status" label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="row.status === 'READY' ? 'success' : row.status === 'COLLECTING' ? 'warning' : 'danger'" size="small">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" :disabled="row.status !== 'READY'" @click="handleDownloadCollect(row)">下载</el-button>
                <el-button size="small" type="danger" plain @click="handleDeleteCollect(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import {
  tailRuntimeLogs, downloadRuntimeLogs, listRuntimeLogFiles,
  queryOperationLogs, exportOperationLogs,
  queryMessageLogs, exportMessageLogs,
  collectLogs, getCollectRecord, listCollectRecords, downloadCollectRecord, deleteCollectRecord,
  type OperationLogRow, type OperationLogPage,
  type MessageLogRow, type MessageLogPage,
  type RuntimeLogFileItem, type CollectRecord,
} from '@/api/logs'

const activeTab = ref('runtime')

// ---------- 运行日志（内核/服务层，不入库 §6.3.2） ----------
const rtSource = ref<'kernel' | 'platform'>('kernel')
const rtAction = ref<'query' | 'download'>('query')

const rtFilter = reactive({ lines: 500, level: '', event_type: '' })
const rtLoading = ref(false)
const rtLinesText = ref('')
async function fetchRuntimeTail() {
  rtLoading.value = true
  try {
    const r = await tailRuntimeLogs({ lines: rtFilter.lines, level: rtFilter.level || undefined, event_type: rtFilter.event_type || undefined, source: rtSource.value })
    if (r.error) { rtLinesText.value = `[失败] ${r.error}`; ElMessage.warning(rtSource.value === 'kernel' ? '内核返回错误' : '服务层返回错误') }
    else { rtLinesText.value = r.lines.join('\n'); ElMessage.success(`共 ${r.total} 行`) }
  } catch (e: any) { rtLinesText.value = `[失败] ${e?.message || e}`; ElMessage.error('查询失败') }
  finally { rtLoading.value = false }
}

const rtFileRange = ref<[string,string]|null>(null)
const rtFileLoading = ref(false)
const runtimeFiles = ref<(RuntimeLogFileItem & { _downloading?: boolean })[]>([])
async function fetchRuntimeFiles() {
  // 用户未选日期范围时不传 start/end，由后端返回全部（不强制默认 7 天过滤）
  const [start, end] = rtFileRange.value || [undefined, undefined]
  rtFileLoading.value = true
  try {
    const files = await listRuntimeLogFiles({ start_date: start, end_date: end, source: rtSource.value })
    runtimeFiles.value = files.map(f => ({ ...f, _downloading: false }))
    ElMessage.success(`查询到 ${files.length} 个日志文件`)
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '查询失败') }
  finally { rtFileLoading.value = false }
}
async function handleDownloadOneRuntime(row: RuntimeLogFileItem & { _downloading?: boolean }) {
  row._downloading = true
  try {
    // 先查询后下载模式：按 filename 下载单个日志文件
    const response = await downloadRuntimeLogs(row.filename, rtSource.value)
    // 拦截器对 blob 响应返回完整 AxiosResponse，需取 .data 得到 Blob
    const blob = (response as any)?.data ?? response
    // 使用纯文件名作为下载文件名（去掉目录层级）
    const downloadName = row.filename.includes('/')
      ? row.filename.substring(row.filename.lastIndexOf('/') + 1) : row.filename
    triggerDownload(blob, downloadName); ElMessage.success('已下载')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '下载失败') }
  finally { row._downloading = false }
}

// ---------- 操作日志 ----------
// 操作类型选项：与后端 AuditLogFilter.parseOperationType 对齐（含 OTHER 兜底）
const OP_TYPE_OPTIONS = [
  { label: '配置操作', value: 'CONFIG_CREATE,CONFIG_UPDATE,CONFIG_DELETE' },
  { label: '记忆操作', value: 'MEMORY_CREATE,MEMORY_UPDATE,MEMORY_DELETE' },
  { label: '变量操作', value: 'VARIABLE_UPDATE,VARIABLE_DELETE' },
  { label: '梦境操作', value: 'DREAMING_START,DREAMING_STOP' },
  { label: '用户登录', value: 'USER_LOGIN' },
  { label: '用户登出', value: 'USER_LOGOUT' },
  { label: '其它', value: 'OTHER' },
]
const opFilter = reactive({ dateRange: null as [string,string]|null, operator_id: '', type: [] as string[], success_only: false, page: 1, size: 20 })
const opLogs = ref<OperationLogRow[]>([]); const opTotal = ref(0)
async function fetchOpLogs() {
  try {
    const r: OperationLogPage = await queryOperationLogs({
      operator_id: opFilter.operator_id || undefined, type: opFilter.type.length ? opFilter.type.join(',') : undefined,
      success_only: opFilter.success_only || undefined,
      start: opFilter.dateRange?.[0] || undefined, end: opFilter.dateRange?.[1] || undefined,
      page: opFilter.page, size: opFilter.size,
    })
    opLogs.value = r.records || []; opTotal.value = r.total || 0
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '查询失败') }
}
async function handleExportOp() {
  if (!opFilter.dateRange || !opFilter.dateRange[0] || !opFilter.dateRange[1]) {
    ElMessage.warning('请选择时间范围（最大7天）'); return
  }
  try {
    const blob = await exportOperationLogs({
      operator_id: opFilter.operator_id || undefined, type: opFilter.type.length ? opFilter.type.join(',') : undefined,
      success_only: opFilter.success_only || undefined,
      start: opFilter.dateRange[0], end: opFilter.dateRange[1],
    })
    triggerDownload(blob, `operation-logs-${new Date().toISOString().slice(0,10)}.csv`); ElMessage.success('已导出')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '导出失败') }
}

// ---------- 消息日志 ----------
const msgFilter = reactive({ dateRange: null as [string,string]|null, user_id: '', scope_id: '', success_only: false, page: 1, size: 20 })
const msgLogs = ref<MessageLogRow[]>([]); const msgTotal = ref(0)
async function fetchMsgLogs() {
  try {
    const r: MessageLogPage = await queryMessageLogs({
      user_id: msgFilter.user_id || undefined, scope_id: msgFilter.scope_id || undefined,
      success_only: msgFilter.success_only || undefined,
      start: msgFilter.dateRange?.[0] || undefined, end: msgFilter.dateRange?.[1] || undefined,
      page: msgFilter.page, size: msgFilter.size,
    })
    msgLogs.value = r.records || []; msgTotal.value = r.total || 0
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '查询失败') }
}
async function handleExportMsg() {
  if (!msgFilter.dateRange || !msgFilter.dateRange[0] || !msgFilter.dateRange[1]) {
    ElMessage.warning('请选择时间范围（最大7天）'); return
  }
  try {
    const blob = await exportMessageLogs({
      user_id: msgFilter.user_id || undefined, scope_id: msgFilter.scope_id || undefined,
      success_only: msgFilter.success_only || undefined,
      start: msgFilter.dateRange[0], end: msgFilter.dateRange[1],
    })
    triggerDownload(blob, `message-logs-${new Date().toISOString().slice(0,10)}.csv`); ElMessage.success('已导出')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '导出失败') }
}

// ---------- 一键采集 ----------
const collectForm = reactive({ scene: '', range: null as [string,string]|null, tenant: '', remark: '' })
const collecting = ref(false)
const collectRecords = ref<CollectRecord[]>([])

async function handleCollect() {
  if (!collectForm.scene) { ElMessage.warning('请选择采集场景'); return }
  if (!collectForm.range) { ElMessage.warning('请选择时间范围'); return }
  const [start, end] = collectForm.range
  collecting.value = true
  try {
    const record = await collectLogs({
      scene: collectForm.scene, start_date: start, end_date: end,
      admin_user_id: collectForm.tenant || undefined, remark: collectForm.remark || undefined,
    })
    // 异步三段式：POST 立即返回 status=COLLECTING，需轮询直到 READY
    ElMessage.success(`采集任务已下发：${record.name}（状态：${record.status}）`)
    await fetchCollectRecords()
    // 启动轮询，3 秒间隔，最多轮询 60 次（3 分钟超时）
    pollCollectStatus(record.id)
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '采集失败') }
  finally { collecting.value = false }
}

/** 轮询采集任务状态，直到 READY/FAILED 或超时 */
const collectPollTimers = ref<Record<string, ReturnType<typeof setInterval>>>({})
function pollCollectStatus(id: string) {
  // 清理已有定时器
  if (collectPollTimers.value[id]) clearInterval(collectPollTimers.value[id])
  let attempts = 0
  const maxAttempts = 60
  collectPollTimers.value[id] = setInterval(async () => {
    attempts++
    try {
      const record = await getCollectRecord(id)
      if (record.status === 'READY') {
        clearInterval(collectPollTimers.value[id])
        delete collectPollTimers.value[id]
        ElMessage.success(`采集完成，可下载：${record.name}`)
        await fetchCollectRecords()
      } else if (record.status === 'FAILED') {
        clearInterval(collectPollTimers.value[id])
        delete collectPollTimers.value[id]
        ElMessage.error(`采集失败：${record.remark || '未知原因'}`)
        await fetchCollectRecords()
      } else if (attempts >= maxAttempts) {
        clearInterval(collectPollTimers.value[id])
        delete collectPollTimers.value[id]
        ElMessage.warning('采集超时，请稍后在列表中查看状态')
        await fetchCollectRecords()
      }
    } catch (e: any) {
      clearInterval(collectPollTimers.value[id])
      delete collectPollTimers.value[id]
      // 404 = 记录已被删除（可能用户在轮询窗口内手动删了），静默停止，不报错
      const status = e?.response?.status
      if (status !== 404) {
        ElMessage.error('轮询采集状态失败')
      }
    }
  }, 3000)
}

async function fetchCollectRecords() {
  try {
    const list = await listCollectRecords({ limit: 100 })
    collectRecords.value = list || []
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '查询失败') }
}

async function handleDownloadCollect(row: CollectRecord) {
  // 异步三段式：必须 status=READY 才能下载
  if (row.status !== 'READY') {
    ElMessage.warning(`采集包尚未就绪，当前状态：${row.status}`)
    return
  }
  try {
    const response = await downloadCollectRecord(row.id)
    // 拦截器对 blob 响应返回完整 AxiosResponse，需取 .data 得到 Blob
    const blob = (response as any)?.data ?? response
    triggerDownload(blob, row.name + '.zip'); ElMessage.success('已下载')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '下载失败') }
}

async function handleDeleteCollect(row: CollectRecord) {
  try {
    await ElMessageBox.confirm(`确认删除采集包「${row.name}」？`, '提示', { type: 'warning' })
  } catch { return }
  try {
    // 删除前先停掉该记录可能仍在运行的轮询定时器，避免删除后还去打已不存在的 id 触发 404
    if (collectPollTimers.value[row.id]) {
      clearInterval(collectPollTimers.value[row.id])
      delete collectPollTimers.value[row.id]
    }
    await deleteCollectRecord(row.id)
    ElMessage.success('已删除')
    await fetchCollectRecords()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '删除失败') }
}

// ---------- 工具 ----------
function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
function formatSize(bytes: number | null): string {
  if (!bytes) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

onMounted(() => {
  fetchOpLogs()
  fetchCollectRecords()
})

// 切换日志来源（内核↔服务层）时清空上一次查询的残留，避免显示错误来源的文件/日志行
watch(rtSource, () => {
  runtimeFiles.value = []
  rtLinesText.value = ''
})

// 消息日志懒加载：仅当用户切换到"消息日志"tab 时才查询内核，避免进入页面就连接内核
const msgLoaded = ref(false)
watch(activeTab, (tab) => {
  if (tab === 'message' && !msgLoaded.value) {
    msgLoaded.value = true
    fetchMsgLogs()
  }
})
</script>

<style scoped>
.level-selector { display: flex; align-items: center; }
.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
</style>
