/**
 * request.ts 拦截器契约测试
 *
 * 覆盖：
 *  - 响应解包 {code,message,data} → data（code=0 通过）
 *  - 业务错误（code!=0）→ reject + ElMessage.error
 *  - HTTP 401 → 清除 localStorage 中的 6 个键 + 跳 /login
 *  - HTTP 404/500 → ElMessage.error
 *  - 网络错误（无 response）→ ElMessage.error
 *  - 请求拦截器：有 token 时注入 Authorization
 *
 * 测试方法：等价类（code=0 / code!=0）+ 决策表（status × 行为）
 *
 * 实现要点：
 *  - vi.mock 工厂会被提升到文件顶部执行，不能引用外部变量（TDZ）。
 *  - 因此 fakeInstance/handlers 通过 vi.hoisted 创建并在工厂内返回。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// 用 vi.hoisted 保证在 mock 提升执行前这些变量已存在
const mockState = vi.hoisted(() => {
  const handlers: {
    requestFulfilled?: (config: any) => any
    responseFulfilled?: (resp: any) => any
    responseRejected?: (err: any) => any
  } = {}
  const fakeInstance = {
    interceptors: {
      request: {
        use: (fulfilled: any) => {
          handlers.requestFulfilled = fulfilled
          return 0
        },
      },
      response: {
        use: (fulfilled: any, rejected: any) => {
          handlers.responseFulfilled = fulfilled
          handlers.responseRejected = rejected
          return 0
        },
      },
    },
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  }
  const elMessageMock = {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  }
  const routerMock = { push: vi.fn() }
  return { handlers, fakeInstance, elMessageMock, routerMock }
})

vi.mock('axios', () => ({
  __esModule: true,
  default: {
    create: () => mockState.fakeInstance,
  },
}))

vi.mock('element-plus', () => ({
  __esModule: true,
  ElMessage: mockState.elMessageMock,
}))

vi.mock('@/router', () => ({
  __esModule: true,
  default: mockState.routerMock,
}))

const { handlers, fakeInstance, elMessageMock, routerMock } = mockState

describe('request.ts — 拦截器契约（决策表）', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    localStorage.clear()
    // 触发模块加载（注册拦截器到 handlers）
    await import('@/api/request')
  })

  it('TC-REQ-001: code=0 时响应拦截器解包返回 data 字段（有效等价类）', () => {
    const payload = { code: 0, message: 'ok', data: { id: 't1', name: 'X' } }
    const result = handlers.responseFulfilled!({ data: payload })
    expect(result).toEqual({ id: 't1', name: 'X' })
  })

  it('TC-REQ-002: code!=0 时响应拦截器 reject 且提示业务消息（无效等价类）', async () => {
    const payload = { code: 50010, message: '用户消息查询 API 未由 :8516 暴露', data: null }
    await expect(
      Promise.resolve().then(() => handlers.responseFulfilled!({ data: payload })),
    ).rejects.toThrow('用户消息查询 API 未由 :8516 暴露')
    expect(elMessageMock.error).toHaveBeenCalledWith('用户消息查询 API 未由 :8516 暴露')
  })

  it('TC-REQ-003: HTTP 401 决策行 — 清除 6 个 localStorage 键并跳 /login', async () => {
    localStorage.setItem('token', 'tok-abc')
    localStorage.setItem('username', 'admin')
    localStorage.setItem('role', 'SUPER_ADMIN')
    localStorage.setItem('tenantId', 'tenant_default')
    localStorage.setItem('scopeIds', '["scope_01"]')
    localStorage.setItem('permissions', '["config:read"]')

    const err = { response: { status: 401, data: { message: 'token 过期' } } }
    await expect(handlers.responseRejected!(err)).rejects.toBe(err)

    expect(localStorage.getItem('token')).toBeNull()
    expect(localStorage.getItem('username')).toBeNull()
    expect(localStorage.getItem('role')).toBeNull()
    expect(localStorage.getItem('tenantId')).toBeNull()
    expect(localStorage.getItem('scopeIds')).toBeNull()
    expect(localStorage.getItem('permissions')).toBeNull()
    expect(routerMock.push).toHaveBeenCalledWith('/login')
    expect(elMessageMock.error).toHaveBeenCalledWith('登录已过期，请重新登录')
  })

  it('TC-REQ-004: HTTP 404 决策行 — 提示"接口不存在"', async () => {
    const err = { response: { status: 404, data: { message: 'not found' } } }
    await expect(handlers.responseRejected!(err)).rejects.toBe(err)
    expect(elMessageMock.error).toHaveBeenCalledWith('请求的接口不存在，请检查API路径')
  })

  it('TC-REQ-005: HTTP 500 + JSON message — 透传服务端 message', async () => {
    const err = { response: { status: 500, data: { message: '内部错误: updates 不能为空' } } }
    await expect(handlers.responseRejected!(err)).rejects.toBe(err)
    expect(elMessageMock.error).toHaveBeenCalledWith('内部错误: updates 不能为空')
  })

  it('TC-REQ-006: HTTP 500 + 字符串响应（HTML 错误页）— 退化为"请求失败 (500)"', async () => {
    const err = { response: { status: 500, data: '<html>Bad Gateway</html>' } }
    await expect(handlers.responseRejected!(err)).rejects.toBe(err)
    expect(elMessageMock.error).toHaveBeenCalledWith('请求失败 (500)')
  })

  it('TC-REQ-007: 网络错误（无 response，仅有 request）— 提示"网络错误"', async () => {
    const err = { request: {} }
    await expect(handlers.responseRejected!(err)).rejects.toBe(err)
    expect(elMessageMock.error).toHaveBeenCalledWith('网络错误,请检查后端服务是否启动')
  })

  it('TC-REQ-008: 请求拦截器 — localStorage 有 token 时注入 Authorization 头', () => {
    localStorage.setItem('token', 'tok-xyz')
    const config: any = { headers: {} }
    const out = handlers.requestFulfilled!(config)
    expect(out.headers.Authorization).toBe('Bearer tok-xyz')
  })

  it('TC-REQ-009: 请求拦截器 — 无 token 时不注入 Authorization 头', () => {
    const config: any = { headers: {} }
    const out = handlers.requestFulfilled!(config)
    expect(out.headers.Authorization).toBeUndefined()
  })

  it('TC-REQ-010: api.get 包装器 — 透传 url/config 到 axios 实例', async () => {
    const { api } = await import('@/api/request')
    fakeInstance.get.mockResolvedValueOnce({ code: 0, data: {} } as any)
    await api.get('/api/v1/config/kernel', { params: { a: 1 } })
    expect(fakeInstance.get).toHaveBeenCalledWith('/api/v1/config/kernel', { params: { a: 1 } })
  })
})
