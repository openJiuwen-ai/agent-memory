import { defineConfig } from 'vitest/config'
import { fileURLToPath, URL } from 'node:url'

// API 层单测不加载 .vue 文件，无需 Vue 插件。
export default defineConfig({
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'node',
    globals: true,
    setupFiles: ['src/api/__tests__/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})
