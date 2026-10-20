/** 前端启动入口：先安装共享状态，再挂载页面；HTTP 令牌已由入口 HTML 注入。 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'

import './styles.css'

createApp(App).use(createPinia()).mount('#app')
