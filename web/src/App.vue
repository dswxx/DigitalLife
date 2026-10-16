<script setup lang="ts">
/**
 * 外壳。**结构就是老界面那个**：
 *
 *   .app
 *   ├── aside.sidebar   左侧栏（品牌 / 新建圆桌 / 会话列表 / 三个导航 / 配色）
 *   └── main.stage      主区（圆桌 / 数字人库 / 模型与密钥）
 *
 * 区别只在主区里放什么：老界面三块，这一版还是那三块，
 * 只是"数字人"多了一层概念（蓝图 → 个体），所以库那一页多一个方块。
 */
import { onMounted, watch } from 'vue'
import { useApp } from './store'
import Sidebar from './components/Sidebar.vue'
import ChatView from './views/ChatView.vue'
import LibraryView from './views/LibraryView.vue'
import SettingsView from './views/SettingsView.vue'
import MemoryModal from './views/MemoryModal.vue'
import TimelineModal from './views/TimelineModal.vue'
import SpeakerModal from './views/SpeakerModal.vue'

const app = useApp()

/** 能写进地址栏的抽屉（打开它 = 换个地址）。席位弹窗不进来：
 *  它属于"正在改桌子"，刷新之后还停在那儿反而碍事。 */
const DEEP = ['memory', 'timeline']

/** 地址栏是**唯一的导航真相**：`#/round` / `#/humans` / `#/settings`，
 *  抽屉再挂一段 `#/humans/memory/<谁>`。
 *  不做的话，在圆桌上按一下刷新就回首页了。 */
function parseHash() {
  const parts = location.hash.replace(/^#\/?/, '').split('?')[0].split('/').filter(Boolean)
  const first = parts[0]
  app.view = first === 'humans' ? 'humans' : first === 'settings' ? 'settings' : 'round'
  if (DEEP.includes(parts[1]) && parts[2]) app.openModal(parts[1] as 'memory' | 'timeline', parts[2])
  else if (DEEP.includes(app.modal)) app.closeModal()
}

/** 反过来：状态一变就把地址栏写上（一个改动入口，不在别处手写 hash）。 */
function syncHash() {
  const want =
    app.modal && DEEP.includes(app.modal) && app.modalLifeId
      ? `#/${app.view}/${app.modal}/${app.modalLifeId}`
      : `#/${app.view}`
  if (location.hash !== want) location.hash = want
}

watch([() => app.view, () => app.modal, () => app.modalLifeId], syncHash)

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
      <div v-if="!app.ready" class="timeline">
        <p class="composer-hint">正在读取…</p>
      </div>

      <div v-else-if="app.problem" class="timeline">
        <div class="answer-note err">连不上后端：{{ app.problem }}</div>
        <p class="composer-hint">
          如果你是刷新页面之后变成这样，多半是服务重启过（令牌变了）——再刷新一次就好。
        </p>
      </div>

      <template v-else>
        <ChatView v-if="app.view === 'round'" />
        <LibraryView v-else-if="app.view === 'humans'" />
        <SettingsView v-else />
      </template>
    </main>

    <MemoryModal v-if="app.modal === 'memory'" />
    <TimelineModal v-if="app.modal === 'timeline'" />
    <SpeakerModal v-if="app.modal === 'speaker'" />
  </div>
</template>
