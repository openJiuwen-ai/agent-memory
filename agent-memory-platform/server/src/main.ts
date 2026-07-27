import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import App from './App.vue'
import router from './router'
import './styles/index.css'

// Element Plus 主题定制 - MemOS 风格
const elementPlusTheme = {
  colorPrimary: '#6366F1',    // Indigo 紫色主色调
  colorSuccess: '#10B981',    // Emerald 绿色
  colorWarning: '#F59E0B',    // Amber 橙色
  colorDanger: '#EF4444',     // Red 红色
  colorInfo: '#6B7280',       // Gray 灰色
}

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { 
  locale: zhCn,
  // 应用自定义主题
})

// 注入主题变量到根元素
document.documentElement.style.setProperty('--el-color-primary', elementPlusTheme.colorPrimary)
document.documentElement.style.setProperty('--el-color-success', elementPlusTheme.colorSuccess)
document.documentElement.style.setProperty('--el-color-warning', elementPlusTheme.colorWarning)
document.documentElement.style.setProperty('--el-color-danger', elementPlusTheme.colorDanger)
document.documentElement.style.setProperty('--el-color-info', elementPlusTheme.colorInfo)

app.mount('#app')
