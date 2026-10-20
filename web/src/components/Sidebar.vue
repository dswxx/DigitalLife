<script setup lang="ts">
/** 侧栏导航、对话列表与主题选择。 */
import { computed, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'

const app = useApp()
const confirmDel = ref('')


const THEMES = [
  { id: 'paper', name: '纸白', desc: '经典白底黑字', ring: '#4a6fd0', fill: '#f5f4f1' },
  { id: 'sepia', name: '米黄', desc: '护眼暖色，像看纸质书', ring: '#a2621f', fill: '#f5eddc' },
  { id: 'mist', name: '雾蓝', desc: '清爽冷色', ring: '#2f7bbf', fill: '#eef2f7' },
  { id: 'matcha', name: '抹茶', desc: '柔和绿，低饱和', ring: '#4c8055', fill: '#eef3ec' },
  { id: 'ink', name: '墨夜', desc: '深色，夜里用', ring: '#7d8ba6', fill: '#22252c' },
]
const theme = ref(localStorage.getItem('digitallife.theme') || '')
const currentTheme = computed(() => THEMES.find((t) => t.id === theme.value) ?? THEMES[0])

function pickTheme(id: string) {
  theme.value = id === 'paper' ? '' : id
  if (theme.value) document.documentElement.setAttribute('data-theme', theme.value)
  else document.documentElement.removeAttribute('data-theme')
  try {
    localStorage.setItem('digitallife.theme', theme.value)
  } catch {
    /* 隐私模式，忽略 */
  }
}

function ago(ms: number): string {
  if (!ms) return '刚刚'
  const diff = Date.now() - ms
  if (diff < 90_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 2 * 86_400_000) return '昨天'
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  const d = new Date(ms)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

async function newTalk() {
  const created = await api.createSession({ title: '新的对话', seats: [] })
  app.sessions = [created, ...app.sessions]
  app.currentSessionId = created.id
  app.syncSeatsFromSession()
  app.clearRound()
  app.go('round')
}

async function openTalk(id: string) {
  app.currentSessionId = id
  app.syncSeatsFromSession()
  app.clearRound()
  app.go('round')
  void app.loadRounds()
}

async function delTalk(id: string) {
  await api.deleteSession(id)
  confirmDel.value = ''
  await app.refreshSessions()
  if (app.currentSessionId === id) {
    app.currentSessionId = app.sessions[0]?.id ?? ''
    app.syncSeatsFromSession()
    app.clearRound()
    await app.loadRounds()
  }
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">🗣️</div>
      <div class="brand-text">
        <b>数字生命</b>
        <span>一次提问 · 多人开口</span>
      </div>
    </div>

    <div class="side-section">
      <button class="btn primary block" @click="newTalk">＋ 新建对话</button>
    </div>

    <div class="thread-list">
      <div
        v-for="s in app.sessions"
        :key="s.id"
        class="thread-item"
        :class="{ active: s.id === app.currentSessionId }"
        @click="openTalk(s.id)"
      >
        <div class="ti-title">{{ s.title || '新的对话' }}</div>
        <div class="ti-sub">
          {{ s.rounds ?? 0 }} 轮 · {{ s.seatCount ?? s.seats.length }} 位 ·
          {{ ago(s.updatedAt) }}
        </div>
        <button
          v-if="confirmDel !== s.id"
          class="ti-del"
          title="删掉这次对话"
          @click.stop="confirmDel = s.id"
        >
          ✕
        </button>
        <button
          v-else
          class="ti-del"
          title="再点一次真的删掉"
          style="color: var(--err)"
          @click.stop="delTalk(s.id)"
        >
          ✓
        </button>
      </div>
      <div v-if="!app.sessions.length" class="hint" style="padding: 10px 4px">
        还没有对话。点上面「＋ 新建对话」。
      </div>
    </div>

    <div class="side-nav">
      <button class="nav-btn" :class="{ active: app.view === 'round' }" @click="app.go('round')">
        <span class="nav-icon">🗣️</span>一问多答
      </button>
      <button class="nav-btn" :class="{ active: app.view === 'humans' }" @click="app.go('humans')">
        <span class="nav-icon">🎭</span>数字人库
      </button>
      <button class="nav-btn" :class="{ active: app.view === 'settings' }" @click="app.go('settings')">
        <span class="nav-icon">🔑</span>模型与密钥
      </button>
    </div>

    <div class="theme-row">
      <div class="theme-label">
        <span>配色</span>
        <span class="theme-current">{{ currentTheme.name }} · {{ currentTheme.desc }}</span>
      </div>
      <div class="theme-swatches">
        <button
          v-for="t in THEMES"
          :key="t.id"
          class="theme-dot"
          :class="{ on: currentTheme.id === t.id }"
          :title="`${t.name} · ${t.desc}`"
          :style="{ borderColor: t.ring, background: t.fill }"
          @click="pickTheme(t.id)"
        >
          <span class="theme-dot-core"></span>
        </button>
      </div>
    </div>
  </aside>
</template>
