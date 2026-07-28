/**
 * vitest node 环境 polyfill
 * request.ts 依赖浏览器 localStorage，node 环境没有，手工注入一个内存实现。
 * 注：node 环境全局没有 Storage 构造器，不能直接 implements/extends。
 */

class MemoryStorage {
  private store = new Map<string, string>()
  get length() {
    return this.store.size
  }
  clear(): void {
    this.store.clear()
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null
  }
  removeItem(key: string): void {
    this.store.delete(key)
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value))
  }
}

;(globalThis as unknown as { localStorage: MemoryStorage }).localStorage = new MemoryStorage()
