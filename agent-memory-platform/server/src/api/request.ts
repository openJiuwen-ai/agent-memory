import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'

// 后端统一响应格式
export interface ApiResponse<T = any> {
  code: number
  message: string
  data: T
}

// 后端断开标志（已废弃，WebSocket心跳监控已接管断连检测）
let isBackendDisconnected = false

const request = axios.create({
  baseURL: '', // API 路径直接写在每个调用中,通过 Vite 代理转发
  timeout: 15000,
})

// 请求拦截器：自动携带 token
request.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

// 响应拦截器：统一错误处理，适配后端 {code, message, data} 格式
request.interceptors.response.use(
  (response) => {
    // 文件下载/导出等非 JSON 响应（blob/arraybuffer/stream）：放行完整响应，
    // 由调用方读取 response.data 作为 Blob 使用。这些响应没有 {code,message,data} 包装。
    const rt = (response.config as any)?.responseType
    if (rt && rt !== 'json') {
      return response
    }

    const res = response.data as ApiResponse

    // 后端返回 code: 0 表示成功
    if (res.code === 0) {
      return res.data // 直接返回 data 字段
    } else {
      // 业务错误
      ElMessage.error(res.message || '请求失败')
      return Promise.reject(new Error(res.message || '请求失败'))
    }
  },
  async (error) => {
    // 处理不同类型的错误
    if (error.response) {
      // 服务器返回了错误响应
      const status = error.response.status
      let data = error.response.data

      // 下载/导出接口的错误体也是 blob，需要解出文本再解析 {code,message}
      if (data instanceof Blob) {
        try {
          const text = (data as any).text ? await (data as Blob).text() : ''
          try { data = JSON.parse(text) } catch { data = text }
        } catch { /* 保持原样 */ }
      }

      // 检查响应是否是JSON格式
      let message = '请求失败'
      if (typeof data === 'object' && data !== null) {
        message = data.message || data.detail || data.error || message
      } else if (typeof data === 'string') {
        // 可能是HTML错误页面
        message = `请求失败 (${status})`
      }

      if (status === 401) {
        ElMessage.error('登录已过期，请重新登录')
        localStorage.removeItem('token')
        localStorage.removeItem('username')
        localStorage.removeItem('role')
        localStorage.removeItem('tenantId')
        localStorage.removeItem('scopeIds')
        localStorage.removeItem('permissions')
        router.push('/login')
      } else if (status === 404) {
        ElMessage.error('请求的接口不存在，请检查API路径')
      } else if (status === 500) {
        // 500 错误：仅提示，不清除登录状态
        // WebSocket 心跳监控会自动检测后端断开并处理退出登录
        console.error('500 错误详情:', {
          message,
          error_code: error.code,
          error_message: error.message
        })
        ElMessage.error('后端服务异常，请检查后端是否正常运行')
      } else if (status === 400) {
        // 400 业务校验错误：不自动弹 toast，把 message 挂到 error 上，
        // 由页面自行决定展示方式（如 ElMessageBox 弹框），避免瞬时 toast 不易阅读。
        error.message = message
      } else {
        ElMessage.error(message)
      }
    } else if (error.request) {
      // 请求已发送但没有收到响应(后端断开/网络错误)
      // 仅提示错误，不清除登录状态
      // WebSocket 心跳监控会自动检测后端断开并处理退出登录
      console.error('后端服务连接失败:', error)
      
      if (!isBackendDisconnected) {
        isBackendDisconnected = true
        ElMessage.error('后端服务已断开，请检查后端是否正常运行')
        
        // 3秒后重置标志，避免频繁提示
        setTimeout(() => {
          isBackendDisconnected = false
        }, 3000)
      }
    } else {
      // 其他错误
      ElMessage.error(error.message || '请求失败')
    }
    
    return Promise.reject(error)
  },
)

export default request

/**
 * 类型化 axios 包装：响应拦截器已 unwrap {code, message, data} → data，
 * 所以运行时 `api.get<T>(url)` 直接返回 `T`，但 axios 静态类型仍是 `AxiosResponse<T>`。
 * 通过 wrapper 把 `AxiosResponse<T>` cast 成 `T`，避免每个 call site 都要 `.then(res => res.data)`。
 */
export const api = {
  get: <T = unknown>(url: string, config?: any) =>
    request.get(url, config) as unknown as Promise<T>,
  post: <T = unknown>(url: string, data?: any, config?: any) =>
    request.post(url, data, config) as unknown as Promise<T>,
  put: <T = unknown>(url: string, data?: any, config?: any) =>
    request.put(url, data, config) as unknown as Promise<T>,
  delete: <T = unknown>(url: string, config?: any) =>
    request.delete(url, config) as unknown as Promise<T>,
}
