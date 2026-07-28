<template>
  <div class="memory-module">
    <el-tabs v-model="activeTab" type="border-card">
      <!-- 记忆浏览 -->
      <el-tab-pane label="记忆浏览" name="browse">
        <MemoryBrowse />
      </el-tab-pane>
      <!-- 记忆搜索 -->
      <el-tab-pane label="记忆搜索" name="search">
        <MemorySearch />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import MemoryBrowse from './MemoryBrowse.vue'
import MemorySearch from './MemorySearch.vue'

const activeTab = ref('browse')
</script>
