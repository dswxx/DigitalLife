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
  const page = location.hash.replace(/^#\/?/, '').split('/')[0]
  app.view = page === 'humans' ? 'humans' : page === 'settings' ? 'settings' : 'round'
}

function syncHash() {
  const hash = `#/${app.view}`
  if (location.hash !== hash) location.hash = hash
}

watch(() => app.view, syncHash)

onMounted(async () => {
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
