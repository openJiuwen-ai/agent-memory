<template>
  <div class="login-container">
    <div class="login-card">
      <!-- 标题区域 -->
      <div class="login-header">
        <div class="login-logo">
          <el-icon :size="40" color="#FFFFFF"><Coin /></el-icon>
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
  /* 蓝紫色渐变背景 */
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  position: relative;
}

.login-card {
  width: 420px;
  padding: 48px 40px;
  background: rgba(255, 255, 255, 0.95);
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  position: relative;
  z-index: 1;
}

.login-header {
  text-align: center;
  margin-bottom: 36px;
}

.login-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 80px;
  height: 80px;
  margin: 0 auto 20px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 50%;
  box-shadow: 0 8px 24px rgba(102, 126, 234, 0.4);
}

.login-title {
  font-size: 26px;
  font-weight: 700;
  color: #333;
  margin-bottom: 6px;
  letter-spacing: 2px;
}

.login-subtitle {
  font-size: 12px;
  color: rgba(0, 0, 0, 0.5);
  letter-spacing: 3px;
  text-transform: uppercase;
  margin-bottom: 8px;
}

.login-slogan {
  font-size: 15px;
  color: #667eea;
  font-weight: 600;
  font-style: italic;
}

.login-options {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
}

/* 输入框样式 */
.login-card :deep(.el-input__wrapper) {
  transition: all 0.3s ease;
  box-shadow: 0 0 0 1px #ddd inset !important;
  background: #fff !important;
}

.login-card :deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px #667eea inset !important;
}

.login-card :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 2px #667eea inset !important;
}

.login-card :deep(.el-input__inner::placeholder) {
  color: #999;
}

.login-card :deep(.el-input__inner) {
  color: #333;
}

.login-btn {
  width: 100%;
  height: 48px;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 8px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
  border: none !important;
  box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4) !important;
  transition: all 0.3s ease !important;
  color: #fff !important;
}

.login-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(102, 126, 234, 0.5) !important;
}

.login-btn:active {
  transform: translateY(0);
}

.login-tip {
  margin-top: 24px;
}

/* 底部波浪装饰 */
.login-footer {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  padding: 20px;
  text-align: center;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.7);
  background: linear-gradient(to top, rgba(0, 0, 0, 0.1), transparent);
}

.login-footer::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(255, 255, 255, 0.3),
    transparent
  );
}
</style>
