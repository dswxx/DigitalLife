<script setup lang="ts">
/**
 * 左侧栏。**结构和老界面一模一样**（品牌 / 新建圆桌 / 会话列表 / 三个导航 / 配色），
 * 只是把"数字人"这层的名字换成了新产品的说法：
 *
 *   老：圆桌 · 数字人库 · 模型与密钥
 *   新：圆桌 · 数字人库 · 模型与密钥   ← 保留（用户认的就是这三个词）
 */
import { computed, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'

const app = useApp()
const confirmDel = ref('')

/** 五套配色。**圆点颜色要内联写**——老界面的 CSS 只画了圆点的形状，
 *  颜色是每个主题自己的主色（老界面也是内联放的）。 */
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

async function newTable() {
  const created = await api.createSession({ title: '新的圆桌', mode: 'parallel', seats: [] })
  app.sessions = [created as any, ...app.sessions]
  app.currentSessionId = created.id
  app.syncSeatsFromSession()
  app.clearRound()
  app.go('round')
}

async function openTable(id: string) {
  app.currentSessionId = id
  app.syncSeatsFromSession()
  app.clearRound()
  app.go('round')
  void app.loadRounds()
}

async function delTable(id: string) {
  await api.deleteSession(id)
  confirmDel.value = ''
  await app.refreshSessions()
  if (app.currentSessionId === id) {
    app.currentSessionId = app.sessions[0]?.id ?? ''
    app.syncSeatsFromSession()
    app.clearRound()
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
      <button class="btn primary block" @click="newTable">＋ 新建圆桌</button>
    </div>

    <div class="thread-list">
      <div
        v-for="s in app.sessions"
        :key="s.id"
        class="thread-item"
        :class="{ active: s.id === app.currentSessionId }"
        @click="openTable(s.id)"
      >
        <div class="ti-title">{{ s.title || '新的圆桌' }}</div>
        <div class="ti-sub">
          {{ (s as any).rounds ?? 0 }} 轮 · {{ (s as any).seatCount ?? s.seats.length }} 位 ·
          {{ ago(s.updatedAt) }}
        </div>
        <button
          v-if="confirmDel !== s.id"
          class="ti-del"
          title="删除这张圆桌"
          @click.stop="confirmDel = s.id"
        >
          ✕
        </button>
        <button
          v-else
          class="ti-del"
          title="再点一次真的删掉"
          style="color: var(--err)"
          @click.stop="delTable(s.id)"
        >
          ✓
        </button>
      </div>
      <div v-if="!app.sessions.length" class="hint" style="padding: 10px 4px">
        还没有桌子。点上面「＋ 新建圆桌」。
      </div>
    </div>

    <div class="side-nav">
      <button class="nav-btn" :class="{ active: app.view === 'round' }" @click="app.go('round')">
        <span class="nav-icon">🗣️</span>圆桌
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
