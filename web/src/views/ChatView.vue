<script setup lang="ts">
/**
 * 圆桌对话。核心观感是**多个席位同时出字**，所以：
 *
 *   · 事件按 `seatNo` 分发到各自的卡片（并排模式下它们是交错的）
 *   · 边收边渲染（不是等整轮算完）
 *   · 思考过程折起来（推理模型会吐很长），但不藏起来
 *   · 黄条在这一轮里不重复（后端已经按会话去重过）
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'
import { renderMarkdown } from '../md'
import type { SeatState } from '../store'

const app = useApp()
const emit = defineEmits<{ (e: 'need-lives'): void }>()
const picking = ref(false)
const openThink = ref<Record<number, boolean>>({})

onMounted(async () => {
  if (!app.currentSessionId && app.sessions.length) app.currentSessionId = app.sessions[0].id
  app.syncSeatsFromSession()
  // 回到这一页就把上次说过的话读回来——不然切走再切回来是空卡片，
  // 看着像"他没说过话"。
  await loadHistory()
})

const session = computed(() => app.currentSession)
const mode = computed(() => session.value?.mode ?? 'parallel')

async function toggleLife(lifeId: string) {
  const current = session.value?.seats.map((s) => ({ lifeId: s.lifeId, model: s.model })) ?? []
  const has = current.some((s) => s.lifeId === lifeId)
  const next = has ? current.filter((s) => s.lifeId !== lifeId) : [...current, { lifeId }]
  await app.setSeats(next)
}

async function setMode(next: 'parallel' | 'council') {
  await app.setMode(next)
}

async function newTable() {
  const created = await api.createSession({ title: '新的圆桌', mode: 'parallel', seats: [] })
  app.sessions = [created, ...app.sessions]
  app.currentSessionId = created.id
  app.syncSeatsFromSession()
}

async function loadHistory() {
  // 把这次会话里"他说过的话"读回来铺在卡片上——不然切走再切回来就空了
  if (!session.value) return
  let lastYouText = ''
  for (const seat of app.seats) {
    try {
      const got = await api.session(seat.lifeId, session.value.id, true)
      const entries = (got.entries ?? []).filter((e: any) => e.kind !== 'summary')
      const lastSelf = [...entries].reverse().find((e: any) => e.kind === 'self')
      const lastYou = [...entries].reverse().find((e: any) => e.kind === 'you')
      seat.text = lastSelf?.text ?? ''
      seat.reasoning = lastSelf?.reasoning ?? ''
      seat.tokens = lastSelf?.tokens ?? 0
      seat.model = lastSelf?.model ?? seat.model
      if (lastYou) {
        seat.notices = []
        lastYouText = lastYou.text
      }
    } catch {
      /* 读不到就空着，不拦着用户提问 */
    }
  }
  if (lastYouText) app.lastQuestion = lastYouText
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault()
    void app.sendQuestion()
  }
}

function thinkLabel(seat: SeatState) {
  return `思考过程 · ${seat.reasoning.length} 字`
}
</script>

<template>
  <div class="row" style="align-items: flex-start">
    <div>
      <h1>圆桌对话</h1>
      <p class="hint">
        {{ session ? `${session.seats.length} 个席位 · ${mode === 'council' ? '会议：按座位顺序，后面的人听得到前面的人' : '并排：同时听到同一句问题，互相听不到这一轮的回答'}` : '还没有桌子' }}
      </p>
    </div>
    <div class="spacer"></div>
    <button class="btn" @click="newTable">＋ 新建圆桌</button>
  </div>

  <!-- ── 席位 ── -->
  <div class="card pad" style="margin-top: 14px">
    <div class="row wrap" style="gap: 10px">
      <div class="seat-strip grow">
        <template v-if="app.seats.length">
          <span v-for="s in app.seats" :key="s.seatNo" class="seat-chip">
            <span>{{ s.emoji }}</span><strong>{{ s.name }}</strong>
            <span class="mono">{{ s.model }}</span>
            <button class="x" title="移出这张桌子" @click="toggleLife(s.lifeId)">✕</button>
          </span>
        </template>
        <span v-else class="hint">这张桌子上还没有人——点右边「加人」</span>
      </div>
      <button class="btn" @click="picking = !picking">加人</button>
      <button class="btn sm" :class="{ primary: mode === 'parallel' }" @click="setMode('parallel')">并排</button>
      <button class="btn sm" :class="{ primary: mode === 'council' }" @click="setMode('council')">会议</button>
    </div>

    <div v-if="picking" style="margin-top: 12px; border-top: 1px dashed var(--line); padding-top: 12px">
      <div v-if="!app.lives.length" class="empty">
        还没有数字人。
        <button class="btn sm" style="margin-left: 8px" @click="emit('need-lives')">去数字人库新建一个</button>
      </div>
      <div v-else class="row wrap" style="gap: 6px">
        <button
          v-for="l in app.lives"
          :key="l.id"
          class="btn sm"
          :class="{ primary: app.seats.some((s) => s.lifeId === l.id) }"
          @click="toggleLife(l.id)"
        >
          {{ l.emoji }} {{ l.name }}
          <span class="mono" style="margin-left: 5px">{{ app.lifeById(l.providerId)?.label || '' }}{{ l.model }}</span>
        </button>
      </div>
    </div>
  </div>

  <!-- ── 上一轮问了什么（问句是对话的一半，不能只有回答）── -->
  <div v-if="app.lastQuestion" class="card pad" style="margin-top: 14px">
    <div class="hint" style="margin-bottom: 4px">上一轮你问的</div>
    <div style="white-space: pre-wrap">{{ app.lastQuestion }}</div>
  </div>

  <!-- ── 回答 ── -->
  <div v-if="app.seats.length" class="answers" style="margin-top: 14px">
    <div v-for="seat in app.seats" :key="seat.seatNo" class="card seat-card">
      <div class="bar">
        <span>{{ seat.emoji }}</span>
        <strong>{{ seat.name }}</strong>
        <span class="model">{{ seat.model }}</span>
        <div class="spacer"></div>
        <span v-if="seat.running" class="tag">作答中…</span>
        <span v-else-if="seat.error" class="tag" style="color: var(--bad-ink); border-color: var(--bad-line)">失败</span>
      </div>

      <div v-if="seat.notices.length" class="notes" style="margin-top: 10px">
        <div v-for="(n, i) in seat.notices" :key="i" class="note">{{ n }}</div>
      </div>

      <details v-if="seat.reasoning" class="think" :open="openThink[seat.seatNo]">
        <summary @click="openThink[seat.seatNo] = !openThink[seat.seatNo]">
          {{ thinkLabel(seat) }}（点开看）
        </summary>
        <div class="txt">{{ seat.reasoning }}</div>
      </details>

      <div v-if="seat.error" class="notes">
        <div class="note bad">{{ seat.error }}</div>
      </div>

      <div class="body">
        <div v-if="seat.text" v-html="renderMarkdown(seat.text)"></div>
        <span v-else-if="seat.running" class="cursor"></span>
        <p v-else-if="!seat.error" class="hint">还没说话。问一句试试。</p>
      </div>

      <div v-if="seat.text || seat.ms || seat.tokens" class="foot">
        <span v-if="seat.ms">{{ (seat.ms / 1000).toFixed(1) }}s</span>
        <span v-if="seat.tokens">{{ seat.tokens }} token 输出</span>
        <span v-if="seat.window">窗口 {{ seat.window }}（{{ seat.keptEntries ?? 0 }} 条原文{{ seat.droppedEntries ? `，砍了 ${seat.droppedEntries}` : '' }}）</span>
        <span v-if="seat.finishReason === 'length'" style="color: var(--warn-ink)">被输出上限截断</span>
      </div>
    </div>
  </div>

  <div v-else class="empty" style="margin-top: 14px">
    先在上面「加人」把要说话的数字人放上桌。
  </div>

  <!-- ── 提问 ── -->
  <div class="askbar">
    <textarea
      v-model="app.question"
      :disabled="!app.seats.length"
      :placeholder="app.seats.length ? '问一句…（Ctrl+Enter 发送）' : '先加人'"
      @keydown="onKey"
    ></textarea>
    <div class="row" style="margin-top: 8px">
      <button class="btn ghost sm" @click="loadHistory">读回这次会话他说过的话</button>
      <div class="spacer"></div>
      <button v-if="app.roundRunning" class="btn danger" @click="app.stop()">停下</button>
      <button class="btn primary" :disabled="!app.seats.length || app.roundRunning || !app.question.trim()" @click="app.sendQuestion()">
        {{ app.roundRunning ? '作答中…' : '提问' }}
      </button>
    </div>
  </div>
</template>
