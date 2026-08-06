<template>
  <div class="trace">
    <el-card shadow="never">
      <template #header>记忆追溯</template>

      <!-- 搜索记忆 -->
      <div class="search-bar">
        <el-select v-model="scopeId" placeholder="Scope" clearable filterable allow-create default-first-option style="width: 200px">
          <el-option v-for="s in scopeOptions" :key="s.scopeId" :label="s.scopeName || s.scopeId" :value="s.scopeId" />
        </el-select>
        <el-input v-model="userId" placeholder="用户 ID" clearable style="width: 140px" />
        <el-input v-model="searchMemId" placeholder="输入记忆ID，如: 019f3f56e5ea456bf8c67fcc" style="width: 340px" clearable @keyup.enter="handleSearch">
          <template #append>
            <el-button type="primary" :icon="Search" @click="handleSearch">追溯</el-button>
          </template>
        </el-input>
      </div>

      <el-empty v-if="!traceData && !loading" description="请输入记忆ID开始追溯" />

      <div v-loading="loading" class="trace-result">
        <template v-if="traceData">
          <!-- 当前状态 -->
          <el-divider content-position="left">当前状态</el-divider>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="记忆ID">{{ traceData.mem_id }}</el-descriptions-item>
            <el-descriptions-item label="类型">{{ traceData.current_state?.memory_type }}</el-descriptions-item>
            <el-descriptions-item label="内容" :span="2">{{ traceData.current_state?.content }}</el-descriptions-item>
            <el-descriptions-item label="Scope">{{ traceData.current_state?.scope_id }}</el-descriptions-item>
            <el-descriptions-item label="用户">{{ traceData.current_state?.user_id }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ traceData.current_state?.created_at }}</el-descriptions-item>
            <el-descriptions-item label="版本">{{ traceData.current_state?.version ?? '-' }}</el-descriptions-item>
          </el-descriptions>

          <!-- 来源追溯 -->
          <el-divider content-position="left">来源追溯</el-divider>
          <el-table v-if="traceData.source_messages?.length" :data="traceData.source_messages" border size="small">
            <el-table-column prop="role" label="角色" width="100" />
            <el-table-column prop="content" label="消息内容" min-width="240" show-overflow-tooltip />
            <el-table-column prop="time" label="时间" width="180" />
          </el-table>
          <el-alert
            v-else
            type="warning"
            :closable="false"
            title="来源消息待补"
            description=":8516 未暴露 get_message_by_id 端点，来源消息暂不可读（F7 §4.4 缺口）。"
            show-icon
          />

          <!-- 变更历史 -->
          <el-divider content-position="left">变更历史</el-divider>
          <el-timeline v-if="traceData.change_history?.length">
            <el-timeline-item
              v-for="(change, index) in traceData.change_history"
              :key="index"
              :timestamp="change.time"
              placement="top"
              :type="index === 0 ? 'primary' : ''"
            >
              <el-card shadow="hover">
                <div class="change-header">
                  <el-tag :type="index === 0 ? 'success' : 'info'" size="small">
                    {{ change.version ?? change.action }}{{ index === 0 ? ' (当前)' : '' }}
                  </el-tag>
                  <span class="operator">{{ change.operator ?? '-' }}</span>
                  <el-tag size="small">{{ change.change_source ?? change.action }}</el-tag>
                </div>
                <div class="change-content">
                  <p><strong>内容:</strong> {{ change.content ?? change.detail }}</p>
                  <p v-if="change.reason"><strong>原因:</strong> {{ change.reason }}</p>
                </div>
              </el-card>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else description="暂无变更记录" />

          <!-- 操作审计 -->
          <el-divider content-position="left">操作审计</el-divider>
          <el-table v-if="traceData.audit_trail?.length" :data="traceData.audit_trail" border size="small">
            <el-table-column prop="time" label="时间" width="180" />
            <el-table-column prop="operation" label="操作" width="100" />
            <el-table-column prop="operator" label="操作人" width="120" />
            <el-table-column prop="operator_type" label="操作类型" width="100" />
            <el-table-column label="结果" width="80">
              <template #default="{ row }">
                <el-tag :type="row.result === '失败' ? 'danger' : 'success'" size="small">
                  {{ row.result ?? '成功' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="detail" label="详情" min-width="200" show-overflow-tooltip />
          </el-table>
          <el-empty v-else description="暂无操作记录" />

          <!-- 血缘关系 -->
          <el-divider content-position="left">血缘关系</el-divider>
          <el-row v-if="traceData.lineage?.parents?.length || traceData.lineage?.children?.length" :gutter="20">
            <el-col :span="12">
              <h4>父节点</h4>
              <el-tag v-for="p in traceData.lineage?.parents" :key="p" style="margin: 4px">{{ p }}</el-tag>
              <span v-if="!traceData.lineage?.parents?.length" class="text-muted">无</span>
            </el-col>
            <el-col :span="12">
              <h4>子节点</h4>
              <el-tag v-for="c in traceData.lineage?.children" :key="c" style="margin: 4px">{{ c }}</el-tag>
              <span v-if="!traceData.lineage?.children?.length" class="text-muted">无</span>
            </el-col>
          </el-row>
          <el-alert
            v-else
            type="info"
            :closable="false"
            title="暂无血缘"
            description="当前 :8516 dreaming 不写血缘（仅记 source_session_id）；待 :8516 在 dreaming store 与写入路径 MemUpdateChecker 埋点后可展示（F7 §4.3）。"
            show-icon
          />
        </template>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { useRoute } from 'vue-router'
import { getTracePage } from '@/api/trace'
import { getAllScopes } from '@/api/scope'
import type { ScopeRegistry } from '@/types/scope'

const route = useRoute()
const searchMemId = ref('')
const scopeId = ref('')
const userId = ref('')
const scopeOptions = ref<ScopeRegistry[]>([])
const traceData = ref<any>(null)
const loading = ref(false)

onMounted(async () => {
  try {
    scopeOptions.value = await getAllScopes()
    if (scopeOptions.value.length > 0 && !scopeId.value) {
      scopeId.value = scopeOptions.value[0].scopeId
    }
  } catch { /* request 拦截器已提示 */ }
})

// 从 MemoryList "查看追溯历史" 跳转来时带 ?memId=xxx&userId=xxx&scopeId=xxx，自动填入并查询
watch(
  () => route.query,
  (q) => {
    const memId = typeof q.memId === 'string' ? q.memId : ''
    if (memId) {
      searchMemId.value = memId
      if (typeof q.userId === 'string' && q.userId) userId.value = q.userId
      if (typeof q.scopeId === 'string' && q.scopeId) scopeId.value = q.scopeId
      handleSearch()
    }
  },
  { immediate: true },
)

async function handleSearch() {
  if (!searchMemId.value) {
    ElMessage.warning('请输入记忆ID')
    return
  }
  loading.value = true
  try {
    traceData.value = await getTracePage(searchMemId.value, userId.value || undefined, scopeId.value || undefined)
    if (!traceData.value) {
      ElMessage.info('未找到该记忆的追溯信息')
    } else {
      ElMessage.success('追溯成功')
    }
  } catch (e: any) {
    traceData.value = null
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.search-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 24px;
}

.trace-result {
  margin-top: 24px;
}

.change-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.operator {
  font-weight: 600;
  color: #303133;
}

.change-content {
  padding: 12px;
  background: #f5f7fa;
  border-radius: 4px;
}

.change-content p {
  margin: 8px 0;
  font-size: 14px;
}

.text-muted {
  color: #909399;
  font-size: 13px;
}

h4 {
  margin: 0 0 12px 0;
  color: #303133;
}
</style>
