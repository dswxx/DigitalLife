/**
 * 全局状态（Pinia）。
 *
 * 只放**跨页面要共享**的东西：启动数据、当前桌子、正在流式的那一轮。
 * 单个页面自己的状态就留在那个组件里——不然这里会变成一个什么都塞的大口袋。
 */

import { defineStore } from 'pinia'
import { api, ask, type Advanced, type LifeSummary, type Provider, type Seat, type Session, type StreamEvent, type Template } from './api'

/** 一位席位在界面上的样子（流式过程也是它）。 */
export interface SeatState {
  seatNo: number
  lifeId: string
  name: string
  emoji: string
  model: string
  text: string
  reasoning: string
  notices: string[]
  error: string
  running: boolean
  ms: number
  tokens: number
  usage: { input?: number; output?: number }
  finishReason: string
  window?: number
  outputCap?: number
  keptEntries?: number
  droppedEntries?: number
}

export const useApp = defineStore('app', {
  state: () => ({
    ready: false,
    problem: '' as string,
    version: '',
    dataDir: '',
    providers: [] as Provider[],
    advanced: {} as Advanced,
    templates: [] as Template[],
    lives: [] as LifeSummary[],
    sessions: [] as Session[],
    memTypes: ['user', 'self', 'feedback', 'reference'] as string[],

    currentSessionId: '' as string,
    question: '',
    /** 上一轮问的是什么。**必须显示出来**：不然刷新回来只看到他在回答，
     *  想不起自己问了什么——问句是这一轮对话的一半。 */
    lastQuestion: '' as string,
    seats: [] as SeatState[],
    roundRunning: false,
    stopRound: null as null | (() => void),

    view: 'library' as 'library' | 'chat' | 'settings',
    // 「记忆」和「一生」是叠在当前页面上的抽屉，不是独立路由——
    // 用户是"看一眼某个数字人的记忆"，不是"离开库去另一个页面"
    drawer: '' as '' | 'memory' | 'timeline' | 'life',
    drawerLifeId: '',
  }),

  getters: {
    lifeById: (s) => (id: string) => s.lives.find((l) => l.id === id),
    templateById: (s) => (id: string) => s.templates.find((t) => t.id === id),
    providerById: (s) => (id: string) => s.providers.find((p) => p.id === id),
    /** 按厂商分组：库的版面是"模板在上、各家厂商的个体在下" */
    livesByProvider(s) {
      const groups = new Map<string, LifeSummary[]>()
      for (const p of s.providers) groups.set(p.id, [])
      for (const l of s.lives) {
        if (!groups.has(l.providerId)) groups.set(l.providerId, [])
        groups.get(l.providerId)!.push(l)
      }
      return [...groups.entries()].map(([providerId, lives]) => ({
        providerId,
        provider: s.providers.find((p) => p.id === providerId),
        lives,
      }))
    },
    templateUsage: (s) => (tid: string) => s.lives.filter((l) => l.templateId === tid).length,
    currentSession: (s) => s.sessions.find((x) => x.id === s.currentSessionId),
  },

  actions: {
    async boot() {
      try {
        const health = await api.health()
        this.version = health.version
        this.dataDir = health.dataDir
        const b = await api.bootstrap()
        this.providers = b.settings.providers
        this.advanced = b.settings.advanced
        this.templates = b.templates
        this.lives = b.lives
        this.sessions = b.sessions
        this.memTypes = b.memTypes
        this.ready = true
        this.problem = ''
      } catch (e) {
        this.problem = e instanceof Error ? e.message : String(e)
      }
    },

    async refreshLives() {
      this.lives = await api.lives()
    },
    async refreshTemplates() {
      this.templates = await api.templates()
    },
    async refreshSessions() {
      this.sessions = await api.sessions()
    },

    openDrawer(kind: 'memory' | 'timeline' | 'life', lifeId: string) {
      this.drawer = kind
      this.drawerLifeId = lifeId
    },
    closeDrawer() {
      this.drawer = ''
      this.drawerLifeId = ''
    },

    /** 开一张桌子：挑好的人和规矩。已经有一张就用它。 */
    async ensureSession(): Promise<Session> {
      if (this.currentSessionId) {
        const found = this.sessions.find((s) => s.id === this.currentSessionId)
        if (found) return found
      }
      if (this.sessions.length) {
        this.currentSessionId = this.sessions[0].id
        this.syncSeatsFromSession()
        return this.sessions[0]
      }
      const created = await api.createSession({ title: '新的圆桌', mode: 'parallel', seats: [] })
      this.sessions = [created, ...this.sessions]
      this.currentSessionId = created.id
      return created
    },

    syncSeatsFromSession() {
      const s = this.currentSession
      this.seats = (s?.seats ?? []).map((seat) => {
        const life = this.lifeById(seat.lifeId)
        return {
          seatNo: seat.seatNo,
          lifeId: seat.lifeId,
          name: life?.name ?? seat.lifeId,
          emoji: life?.emoji ?? '🙂',
          model: seat.model || life?.model || '',
          text: '',
          reasoning: '',
          notices: [],
          error: '',
          running: false,
          ms: 0,
          tokens: 0,
          usage: {},
          finishReason: '',
        }
      })
    },

    async setSeats(next: Array<{ lifeId: string; model?: string }>, mode?: 'parallel' | 'council') {
      const session = await this.ensureSession()
      const seats: Seat[] = next.map((x, i) => {
        const life = this.lifeById(x.lifeId)
        return {
          seatNo: i + 1,
          lifeId: x.lifeId,
          providerId: life?.providerId ?? '',
          model: x.model ?? life?.model ?? '',
          temperature: null,
        }
      })
      const updated = await api.updateSession(session.id, { seats, mode: mode ?? session.mode })
      this.sessions = this.sessions.map((s) => (s.id === updated.id ? updated : s))
      this.syncSeatsFromSession()
    },

    async setMode(mode: 'parallel' | 'council') {
      const session = await this.ensureSession()
      const updated = await api.updateSession(session.id, { mode })
      this.sessions = this.sessions.map((s) => (s.id === updated.id ? updated : s))
    },

    /** 问一句。事件是按 seatNo 分发的——并排模式下它们是交错的。 */
    async sendQuestion() {
      const q = this.question.trim()
      if (!q || this.roundRunning || !this.seats.length) return
      const session = await this.ensureSession()
      for (const seat of this.seats) {
        seat.text = ''
        seat.reasoning = ''
        seat.notices = []
        seat.error = ''
        seat.running = false
        seat.ms = 0
        seat.tokens = 0
        seat.finishReason = ''
      }
      this.roundRunning = true
      this.lastQuestion = q
      this.question = ''
      const { done, stop } = ask({ sessionId: session.id, question: q }, (evt) => this.applyEvent(evt))
      this.stopRound = stop
      try {
        await done
      } finally {
        this.roundRunning = false
        this.stopRound = null
        for (const seat of this.seats) {
          if (seat.running) {
            // 流断了但没收到 seat_done：不能让卡片永远转圈
            seat.running = false
            seat.error = seat.error || '这一轮没有正常收尾（可能是连接断了，或者你点了停）'
          }
        }
        this.refreshLives().catch(() => {})
      }
    },

    applyEvent(evt: StreamEvent) {
      const seat = this.seats.find((s) => s.seatNo === evt.seatNo)
      switch (evt.type) {
        case 'seat_start':
          if (seat) {
            seat.running = true
            seat.name = evt.name ?? seat.name
          }
          break
        case 'text':
          if (seat) seat.text += evt.text ?? ''
          break
        case 'reasoning':
          if (seat) seat.reasoning += evt.text ?? ''
          break
        case 'notice':
        case 'limit':
          if (seat && evt.text && !seat.notices.includes(evt.text)) seat.notices.push(evt.text)
          break
        case 'seat_done':
          if (seat) {
            seat.running = false
            seat.text = evt.content ?? seat.text
            seat.reasoning = evt.reasoning ?? seat.reasoning
            seat.error = evt.error ?? ''
            seat.ms = evt.ms ?? 0
            seat.tokens = evt.tokens ?? 0
            seat.usage = evt.usage ?? {}
            seat.finishReason = evt.finishReason ?? ''
            seat.window = evt.window
            seat.outputCap = evt.outputCap
            seat.keptEntries = evt.keptEntries
            seat.droppedEntries = evt.droppedEntries
            if (evt.notices) for (const n of evt.notices) if (!seat.notices.includes(n)) seat.notices.push(n)
            seat.model = evt.model ?? seat.model
          }
          break
        case 'error':
          if (seat) {
            seat.running = false
            seat.error = evt.message ?? '出错了'
          } else if (evt.message) {
            // 没有 seatNo 的错误是整轮的（比如找不到桌子）
            this.problem = evt.message
          }
          break
        default:
          break
      }
    },

    stop() {
      this.stopRound?.()
      this.roundRunning = false
    },
  },
})
