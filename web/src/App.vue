<script setup lang="ts">
/** 应用外壳与 hash 路由同步，挂载页面及全局弹窗。 */
import { onMounted, watch } from 'vue'
import { useApp } from './store'
import Sidebar from './components/Sidebar.vue'
import ChatView from './views/ChatView.vue'
import LibraryView from './views/LibraryView.vue'
import SettingsView from './views/SettingsView.vue'
import SpeakerModal from './components/SpeakerModal.vue'

const app = useApp()

function parseHash() {
  // 地址栏仅承载三个主页面，席位弹窗属于临时编辑状态，不写入路由。
  const page = location.hash.replace(/^#\/?/, '').split('/')[0]
  app.view = page === 'humans' ? 'humans' : page === 'settings' ? 'settings' : 'round'
}

function syncHash() {
  // 双向同步先比较值，避免状态变化与 hashchange 相互重复触发导航。
  const hash = `#/${app.view}`
  if (location.hash !== hash) location.hash = hash
}

watch(() => app.view, syncHash)

onMounted(async () => {
  // 先还原用户刷新前所在页面，再加载页面依赖的共享概况。
  parseHash()
  window.addEventListener('hashchange', parseHash)
  await app.boot()
  syncHash()
})
</script>

<template>
  <div class="app">
    <Sidebar />

    <main class="stage">
      <div v-if="app.problem" class="timeline">
        <div class="answer-note err">{{ app.problem }}</div>
        <p class="composer-hint">请检查服务是否运行，或刷新页面重新连接。</p>
      </div>

      <div v-else-if="!app.ready" class="timeline">
        <p class="composer-hint">正在读取…</p>
      </div>

      <template v-else>
        <ChatView v-if="app.view === 'round'" />
        <LibraryView v-else-if="app.view === 'humans'" />
        <SettingsView v-else />
      </template>
    </main>

    <SpeakerModal v-if="app.modal === 'speaker'" />
  </div>
</template>
