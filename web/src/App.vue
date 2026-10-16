<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useApp } from './store'
import LibraryView from './views/LibraryView.vue'
import ChatView from './views/ChatView.vue'
import SettingsView from './views/SettingsView.vue'
import MemoryDrawer from './views/MemoryDrawer.vue'
import TimelineDrawer from './views/TimelineDrawer.vue'

const app = useApp()
const tab = ref<'library' | 'chat' | 'settings'>('library')

/**
 * 地址栏里记住"我在哪一页"（`#/chat`、`#/library`、`#/life/<id>/memory`…）。
 *
 * 不做的话，用户在对话页按一下刷新就回到首页、正在看的那位数字人的记忆也关了——
 * 这种"随手刷一下就丢"很难解释。
 */
function parseHash() {
  // 去掉可能挂在 hash 里的查询串（`#/chat?_=123`）——防缓存的参数有可能拼在这儿
  const clean = location.hash.replace(/^#\/?/, '').split('?')[0]
  const parts = clean.split('/').filter(Boolean)
  const first = parts[0] ?? 'library'
  tab.value = first === 'chat' ? 'chat' : first === 'settings' ? 'settings' : 'library'
  app.view = tab.value
  if (first === 'life' && parts[1]) {
    const kind = parts[2] === 'timeline' ? 'timeline' : 'memory'
    app.openDrawer(kind, parts[1])
  } else {
    app.closeDrawer()
  }
}

function setHash(next: string) {
  if (location.hash !== next) location.hash = next
  else parseHash()
}

function go(where: 'library' | 'chat' | 'settings') {
  setHash(`#/${where}`)
}

watch(
  () => [app.drawer, app.drawerLifeId],
  ([kind, id]) => {
    if (kind && id) setHash(`#/life/${id}/${kind}`)
    else if (tab.value === 'library' && app.drawer === '' && location.hash.startsWith('#/life')) setHash('#/library')
  },
)

onMounted(async () => {
  parseHash()
  window.addEventListener('hashchange', parseHash)
  await app.boot()
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">数字生命 <small>DigitalLife</small></div>
      <nav class="tabs">
        <button class="tab" :class="{ on: tab === 'library' }" @click="go('library')">数字人库</button>
        <button class="tab" :class="{ on: tab === 'chat' }" @click="go('chat')">圆桌对话</button>
        <button class="tab" :class="{ on: tab === 'settings' }" @click="go('settings')">设置</button>
      </nav>
      <div class="spacer"></div>
      <div class="meta">
        <template v-if="app.ready">{{ app.version }} · 数据 v{{ app.dataVersion }}</template>
        <template v-else-if="app.problem">连不上</template>
      </div>
    </header>

    <main class="main">
      <div class="wrap">
        <div v-if="app.problem" class="note bad" style="margin-bottom: 14px">
          <strong>连不上后端：</strong>{{ app.problem }}
          <div class="hint">
            如果你是刷新页面之后变成这样，多半是服务重启过（令牌变了）——再刷新一次就好。
          </div>
        </div>
        <div v-else-if="!app.ready" class="empty">正在读取…</div>

        <template v-else>
          <LibraryView v-if="tab === 'library'" @open-chat="go('chat')" />
          <ChatView v-else-if="tab === 'chat'" @need-lives="go('library')" />
          <SettingsView v-else />
        </template>
      </div>
    </main>

    <MemoryDrawer v-if="app.drawer === 'memory'" :life-id="app.drawerLifeId" @close="app.closeDrawer()" />
    <TimelineDrawer v-if="app.drawer === 'timeline'" :life-id="app.drawerLifeId" @close="app.closeDrawer()" />
  </div>
</template>
