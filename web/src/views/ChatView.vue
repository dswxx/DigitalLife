<script setup lang="ts">
/** 一问多答页面：提问对象、问答轮次、流式回答和输入区。 */
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import { useApp } from '../store'
import { renderMarkdown } from '../md'

const app = useApp()
const title = ref('')

const session = computed(() => app.currentSession)

const EXAMPLES = [
  '我三十三岁，想辞掉稳定工作去写小说，你怎么看？',
  '读了很多书却记不住，问题出在哪？',
  '人到中年还该不该追求「不平庸」？',
]

onMounted(async () => {
  if (!app.sessions.length) await app.refreshSessions()
  if (!app.currentSessionId && app.sessions.length) {
    app.currentSessionId = app.sessions[0].id
    app.syncSeatsFromSession()
  }
  title.value = session.value?.title ?? '新的对话'
  if (!app.rounds.length) await app.loadRounds()
})

watch(
  () => app.currentSessionId,
  () => {
    title.value = session.value?.title ?? '新的对话'
  },
)

async function rename() {
  const s = session.value
  if (!s) return
  const updated = await api.updateSession(s.id, { title: title.value || '新的对话' })
  app.sessions = app.sessions.map((x) => (x.id === updated.id ? updated : x))
}

function useExample(text: string) {
  app.question = text
}

function onKey(e: KeyboardEvent) {

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    void app.sendQuestion()
  }
}

function fmtTime(ms: number) {
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`
}
</script>

<template>
  <div class="thread-head">
    <input
      v-model="title"
      class="thread-title-input"
      maxlength="40"
      :disabled="!session"
      :title="
        session
          ? '点一下可以改名字'
          : '还没选人。加一位进来，这次对话才真的存在，那时候再改名'
      "
      @change="rename"
    />
    <span class="pill">🗣️ {{ app.seats.length }} 位</span>
    <div style="flex: 1"></div>
  </div>

  <div class="seat-bar">
    <div
      v-for="seat in app.seats"
      :key="seat.seatNo"
      class="seat-chip"
      title="点一下改这位的配置"
      @click="!app.roundRunning && app.openModal('speaker', String(seat.seatNo))"
    >
      <span class="av">{{ seat.emoji }}</span>
      <div class="meta">
        <div class="nm">{{ seat.name }}</div>
        <div class="md">{{ seat.model || '没选模型' }}</div>
      </div>
      <button class="seat-del" :disabled="app.roundRunning" title="这次不问他" @click.stop="app.removeSeat(seat.seatNo)">✕</button>
    </div>
    <button class="seat-chip add" :disabled="app.roundRunning" @click="app.openModal('speaker', '0')">＋ 加一位</button>
  </div>

  <div class="timeline">
    <button v-if="app.hasMoreRounds" class="btn" :disabled="app.loading" @click="app.loadRounds(10, true)">
      {{ app.loading ? '正在读取…' : '加载更早的对话' }}
    </button>
    <!-- 空状态 -->
    <div v-if="!app.rounds.length && !app.loading" class="empty">
      <div class="big">🎙️</div>
      <h3>一问多答：问一句，他们各自答</h3>
      <p>
        {{
          app.seats.length
            ? '同一个问题会同时送到下面每一位那里，谁先想清楚谁先开口。'
            : '先从上面「＋ 加一位」把数字人叫来。'
        }}
      </p>
      <div class="row wrap" style="justify-content: center; margin-top: 14px">
        <button v-for="(ex, i) in EXAMPLES" :key="i" class="btn tiny" @click="useExample(ex)">
          {{ ex }}
        </button>
      </div>
    </div>

    <div v-for="rnd in app.rounds" :key="rnd.id || rnd.t" class="round">
      <div class="ask-bubble">
        <div class="inner">{{ rnd.question }}</div>
      </div>
      <div class="ask-meta">{{ fmtTime(rnd.t) }}</div>

      <div class="answers">
        <div v-for="a in rnd.answers" :key="a.seatNo" class="answer" :class="{ err: !!a.error }">
          <div class="answer-head">
            <span class="av">{{ a.emoji }}</span>
            <div>
              <div class="nm">{{ a.name }}</div>
              <div class="sub">{{ a.model }}</div>
            </div>
            <div class="spacer"></div>
            <span v-if="a.running" class="pill">作答中…</span>
          </div>

          <div v-for="(n, i) in a.notices" :key="i" class="answer-note">⚠ {{ n }}</div>

          <details v-if="a.reasoning" class="think">
            <summary>思考过程 · {{ a.reasoning.length }} 字</summary>
            <div class="think-body">{{ a.reasoning }}</div>
          </details>

          <div v-if="a.error" class="answer-note err">✕ {{ a.error }}</div>

          <div class="answer-body">
            <div v-if="a.text" class="md" v-html="renderMarkdown(a.text)"></div>
            <span v-else-if="a.running" class="cursor">▍</span>
            <p v-else-if="!a.error" class="composer-hint" style="margin: 0">还没说话。</p>
          </div>

          <div v-if="a.text || a.ms || a.tokens" class="answer-foot">
            <span v-if="a.ms">{{ (a.ms / 1000).toFixed(1) }}s</span>
            <span v-if="a.tokens">{{ a.tokens }} token</span>
            <div class="spacer"></div>
            <span v-if="a.finishReason === 'length'" style="color: var(--warn)">
              被输出上限截断
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="composer">
    <div class="composer-inner">
      <textarea
        v-model="app.question"
        :disabled="!app.seats.length"
        :placeholder="
          app.seats.length
            ? '把你的问题写在这里，一次发给这几位……'
            : '先从上面加一位，再开口'
        "
        @keydown="onKey"
      ></textarea>
      <button v-if="app.roundRunning" class="btn danger" @click="app.stop()">停下</button>
      <button
        v-else
        class="btn primary"
        :disabled="!app.seats.length || !app.question.trim()"
        @click="app.sendQuestion()"
      >
        发给这几位
      </button>
    </div>
    <div class="composer-hint">
      <span v-if="!app.seats.length">⚠ 还没选人，点上面的「＋ 加一位」</span>
      <span v-else>
        他们同时答，互相听不到对方 · Enter 发送 · Shift+Enter 换行
      </span>
      <div class="spacer"></div>
      <span>历史：{{ app.rounds.length }} 轮</span>
    </div>
  </div>
</template>
