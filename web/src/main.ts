import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'

// 老界面那份样式**原样搬过来、一个字没改**——这一版的目标就是"骨架还是老样子"，
// 有它在，类名、间距、控件长相就有了基准，不用我凭印象重画。
import './styles/old-app.css'
// 新东西（蓝图卡 / 记忆编辑器 / 一生 / 步骤条）用同一套变量写在这里，看着是同一套
import './styles/extra.css'

createApp(App).use(createPinia()).mount('#app')
