/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface ImportMetaEnv {
  readonly VITE_APP_TITLE: string
  readonly VITE_BACKEND_API_BASE_URL: string
  readonly VITE_AUTH_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
