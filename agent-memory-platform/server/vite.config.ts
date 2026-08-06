/**
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    // Vite 插件配置
    plugins: [
      vue(),
      // 自动导入 Element Plus 组件
      Components({
        resolvers: [ElementPlusResolver()],
      }),
      // 自动导入 Element Plus API
      AutoImport({
        resolvers: [ElementPlusResolver()],
      }),
    ],
    // 路径别名配置
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    // 开发服务器配置
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        '/api': {
          changeOrigin: true,
          target: env.VITE_BACKEND_API_BASE_URL || 'http://localhost:9000',
        },
        '/ws': {
          target: env.VITE_BACKEND_API_BASE_URL?.replace('http', 'ws').replace('https', 'wss') || 'ws://localhost:9000',
          ws: true,
          changeOrigin: true,
        },
      },
    },
  }
})
