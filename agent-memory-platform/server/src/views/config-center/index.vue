<!--
  配置中心 — 2026-07-19 P0-3 v3 重构
  Tab 结构（2 个）：
  1. 配置模板 — 系统默认（is_builtin）+ 自定义（is_builtin=false），
     支持 SCOPE（应用到租户）+ INSTANCE（实例单例）两种类型。
     INSTANCE 预置模板分热启动（立即生效）与冷启动（需重启）。
     含租户级 Scope 配置快照列表（应用结果）。
  2. 内核配置 — 只读展示安装参数 + 连接参数（全局默认值）。
-->
<template>
  <div class="config-center">
    <el-tabs v-model="activeTab" type="card" class="config-tabs">
      <el-tab-pane label="配置模板" name="template">
        <TemplateList />
      </el-tab-pane>

      <el-tab-pane label="内核配置" name="kernel">
        <KernelConfig />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import TemplateList from './TemplateList.vue'
import KernelConfig from './KernelConfig.vue'

const activeTab = ref('template')
</script>

<style scoped>
.config-center {
  padding: 16px;
}
.config-tabs {
  background: #fff;
  border-radius: 4px;
  padding: 12px;
}
</style>
