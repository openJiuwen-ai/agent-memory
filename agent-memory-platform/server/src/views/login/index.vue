<template>
  <div class="login-container">
    <div class="login-card">
      <!-- 标题区域 -->
      <div class="login-header">
        <div class="login-logo">
          <el-icon :size="40" color="#6366F1"><Coin /></el-icon>
        </div>
        <h1 class="login-title">记忆管理平台</h1>
        <p class="login-subtitle">Memory Management Platform</p>
      </div>

      <!-- 登录表单 -->
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        size="large"
        @keyup.enter="handleLogin"
      >
        <el-form-item prop="username">
          <el-input
            v-model="form.username"
            :prefix-icon="User"
            placeholder="请输入用户名"
            clearable
          />
        </el-form-item>

        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            :prefix-icon="Lock"
            type="password"
            show-password
            placeholder="请输入密码"
            clearable
          />
        </el-form-item>

        <div class="login-options">
          <el-checkbox v-model="form.remember">
            记住我
          </el-checkbox>
          <el-link type="primary" :underline="false">
            忘记密码?
          </el-link>
        </div>

        <el-button
          type="primary"
          class="login-btn"
          :loading="loading"
          @click="handleLogin"
        >
          登 录
        </el-button>
      </el-form>

      <!-- 提示信息 -->
      <el-alert
        class="login-tip"
        type="info"
        :closable="false"
        show-icon
      >
        <template #title>
          演示账号：<strong>admin</strong> / 密码：<strong>admin123</strong>
        </template>
      </el-alert>
    </div>

    <!-- 底部版权 -->
    <div class="login-footer">
      Copyright © 2026 Contributors
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Coin } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
  username: '',
  password: '',
  remember: false,
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 20, message: '用户名长度为 3-20 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度为 6-20 个字符', trigger: 'blur' },
  ],
}

async function handleLogin() {
  if (!formRef.value) return

  await formRef.value.validate(async (valid) => {
    if (!valid) return

    loading.value = true
    try {
      await userStore.login({
        username: form.username,
        password: form.password,
      })

      // 记住我：保存用户名
      if (form.remember) {
        localStorage.setItem('remembered_username', form.username)
      } else {
        localStorage.removeItem('remembered_username')
      }

      ElMessage.success('登录成功')
      router.push('/dashboard')
    } catch {
      // 错误已由响应拦截器处理
    } finally {
      loading.value = false
    }
  })
}

// 初始化：恢复记住的用户名
const remembered = localStorage.getItem('remembered_username')
if (remembered) {
  form.username = remembered
  form.remember = true
}
</script>

<style scoped>
.login-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: #F8F9FA;
  position: relative;
}

/* 添加科技感背景装饰 */
.login-container::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: 
    radial-gradient(circle at 20% 50%, rgba(99, 102, 241, 0.03) 0%, transparent 50%),
    radial-gradient(circle at 80% 80%, rgba(99, 102, 241, 0.03) 0%, transparent 50%);
  pointer-events: none;
}

.login-card {
  width: 420px;
  padding: 40px 36px;
  background: #FFFFFF;
  border-radius: 12px;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
  border: 1px solid #E5E7EB;
  position: relative;
  z-index: 1;
}

.login-header {
  text-align: center;
  margin-bottom: 32px;
}

.login-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 72px;
  height: 72px;
  margin: 0 auto 16px;
  background: #EEF2FF;
  border-radius: 50%;
}

.login-title {
  font-size: 24px;
  font-weight: 600;
  color: #1F2937;
  margin-bottom: 4px;
}

.login-subtitle {
  font-size: 13px;
  color: #6B7280;
  letter-spacing: 1px;
}

.login-options {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}

.login-btn {
  width: 100%;
  height: 44px;
  font-size: 16px;
  letter-spacing: 4px;
  background-color: #6366F1 !important;
  border-color: #6366F1 !important;
}

.login-btn:hover {
  background-color: #4F46E5 !important;
  border-color: #4F46E5 !important;
}

.login-tip {
  margin-top: 20px;
}

.login-footer {
  position: absolute;
  bottom: 20px;
  font-size: 12px;
  color: #9CA3AF;
}
</style>
