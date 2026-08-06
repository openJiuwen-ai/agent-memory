<template>
  <div class="memory-browse">
    <!-- 筛选栏 -->
    <div class="filter-bar">
      <el-select v-model="query.scope_id" placeholder="Scope" clearable filterable allow-create default-first-option style="width: 200px">
        <el-option v-for="s in scopeOptions" :key="s.scopeId" :label="s.scopeName || s.scopeId" :value="s.scopeId" />
      </el-select>
      <el-input v-model="query.user_id" placeholder="用户 ID" clearable style="width: 140px" />
      <el-select v-model="query.memory_type" placeholder="记忆类型" clearable style="width: 140px">
        <el-option label="用户画像" value="user_profile" />
        <el-option label="语义记忆" value="semantic_memory" />
        <el-option label="情景记忆" value="episodic_memory" />
        <el-option label="摘要" value="summary" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="fetchList">搜索</el-button>
      <el-button :icon="RefreshRight" @click="handleReset">重置</el-button>
    </div>

    <!-- 记忆列表 -->
    <el-table v-loading="loading" :data="memories" border>
      <el-table-column prop="mem_id" label="记忆ID" width="180" />
      <el-table-column prop="memory_type" label="类型" width="120">
        <template #default="{ row }">
          <el-tag size="small">{{ typeText(row.memory_type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="content" label="内容" min-width="300" show-overflow-tooltip />
      <el-table-column prop="scope_id" label="Scope" width="140" />
      <el-table-column prop="user_id" label="用户ID" width="100" />
      <el-table-column prop="updated_at" label="更新时间" width="170" />
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button type="primary" link @click="handleEdit(row)" :disabled="!userStore.hasPermission('memory:write')">修改</el-button>
          <el-button type="danger" link @click="handleDelete(row)" :disabled="!userStore.hasPermission('memory:delete')">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无记忆数据" />
      </template>
    </el-table>

    <el-pagination
      style="margin-top: 16px; justify-content: flex-end"
      :current-page="query.page_idx + 1"
      :page-size="query.page_size"
      :total="total"
      :page-sizes="[10, 20, 50]"
      layout="total, sizes, prev, pager, next"
      @current-change="handlePageChange"
      @size-change="handleSizeChange"
    />

    <!-- 修改记忆弹框 -->
    <el-dialog v-model="editVisible" title="修改记忆" width="520px">
      <el-form :model="editForm" label-width="80px">
        <el-form-item label="记忆内容">
          <el-input v-model="editForm.content" type="textarea" :rows="4" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmitEdit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, RefreshRight } from '@element-plus/icons-vue'
import type { MemoryRecord, MemoryType } from '@/types/memory'
import { browseMemories, deleteMemory, updateMemory } from '@/api/memory'
import { getAllScopes } from '@/api/scope'
import type { ScopeRegistry } from '@/types/scope'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

const loading = ref(false)
const memories = ref<MemoryRecord[]>([])
const total = ref(0)
const scopeOptions = ref<ScopeRegistry[]>([])

const query = reactive({
  scope_id: '',
  user_id: '',
  memory_type: '' as MemoryType | '',
  page_idx: 0,
  page_size: 10,
})

async function fetchList() {
  loading.value = true
  try {
    const res = await browseMemories({ ...query })
    memories.value = res.memories
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function handlePageChange(page: number) {
  query.page_idx = page - 1
  fetchList()
}

function handleSizeChange() {
  query.page_idx = 0
  fetchList()
}

function handleReset() {
  query.scope_id = ''
  query.user_id = ''
  query.memory_type = ''
  query.page_idx = 0
  fetchList()
}

function typeText(type: MemoryType): string {
  const map: Record<MemoryType, string> = {
    user_profile: '用户画像',
    semantic_memory: '语义记忆',
    episodic_memory: '情景记忆',
    summary: '摘要',
    variable: '变量',
  }
  return map[type]
}

const editVisible = ref(false)
const submitting = ref(false)
const editForm = reactive({ mem_id: '', content: '', user_id: '', scope_id: '' })

function handleEdit(row: MemoryRecord) {
  editForm.mem_id = row.mem_id
  editForm.content = row.content
  editForm.user_id = row.user_id
  editForm.scope_id = row.scope_id
  editVisible.value = true
}

async function handleSubmitEdit() {
  submitting.value = true
  try {
    await updateMemory(editForm.mem_id, editForm.content, editForm.user_id, editForm.scope_id)
    ElMessage.success('修改成功')
    editVisible.value = false
    fetchList()
  } catch (e: any) {
    // 错误已由 request 拦截器提示
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row: MemoryRecord) {
  try {
    await ElMessageBox.confirm('确定要删除该记忆吗？', '提示', { type: 'warning' })
    await deleteMemory(row.mem_id, row.user_id, row.scope_id)
    ElMessage.success('删除成功')
    fetchList()
  } catch (e: any) {
    // 取消删除或错误（错误已由拦截器提示）
  }
}

onMounted(async () => {
  try {
    scopeOptions.value = await getAllScopes()
    if (scopeOptions.value.length > 0) {
      query.scope_id = scopeOptions.value[0].scopeId
    }
  } catch { /* request 拦截器已提示 */ }
  fetchList()
})
</script>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
</style>
