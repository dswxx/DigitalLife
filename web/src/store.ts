/**
 * 全局状态（Pinia）。
 *
 * 只放**跨页面要共享**的东西：启动数据、当前桌子、桌上的几轮对话、正在流式的那一轮。
 * 单个页面自己的状态就留在那个组件里——不然这里会变成一个什么都塞的大口袋。
 *
 * 和上一版的区别：席位是**配置**（谁坐在桌上），回答是**轮次**（一轮一轮往下堆）。
 * 老界面就是这么分的，也更好用：往上翻能看到之前问过什么、谁怎么答的。
 */

import { defineStore } from 'pinia'
import { api, ask, type Advanced, type LifeSummary, type Provider, type Seat, type Session, type StreamEvent, type Template } from './api'

/** 一位席位（桌上有谁）。 */
export interface SeatConfig {
  seatNo: number
  lifeId: string
  name: string
  emoji: string
  model: string
  providerLabel: string
}

/** 一位席位在某一轮里的回答。 */
export interface AnswerView {
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

/** 一轮对话。 */
export interface RoundView {
  t: number
  question: string
  answers: AnswerView[]
}

export const useApp = defineStore('app', {
  state: () => ({
    ready: false,
    problem: '' as string,
    version: '',
    dataVersion: 0,
    dataDir: '',
    providers: [] as Provider[],
    advanced: {} as Advanced,
    templates: [] as Template[],
    lives: [] as LifeSummary[],
    sessions: [] as Session[],
    memTypes: ['user', 'self', 'feedback', 'reference'] as string[],

    /** 老界面的三个视图，名字也照旧 */
    view: 'round' as 'round' | 'humans' | 'settings',
    currentSessionId: '' as string,
    question: '',
    seats: [] as SeatConfig[],
    rounds: [] as RoundView[],
    roundRunning: false,
    stopRound: null as null | (() => void),
    loading: false,

    // 「记忆」「一生」是盖在页面上的弹窗，不是独立页面
    modal: '' as '' | 'memory' | 'timeline' | 'life' | 'speaker',
    modalLifeId: '',
    /** 席位弹窗编的是第几号座位（0 = 新加一个） */
    modalSeat: 0,
  }),

  getters: {
    lifeById: (s) => (id: string) => s.lives.find((l) => l.id === id),
    templateById: (s) => (id: string) => s.templates.find((t) => t.id === id),
    providerById: (s) => (id: string) => s.providers.find((p) => p.id === id),
    /** 按厂商分组：库的版面是"一家一个方块，方块里是他的个体" */
    livesByProvider(s) {
      const groups = new Map<string, LifeSummary[]>()
      for (const p of s.providers) groups.set(p.id, [])
      for (const l of s.lives) {
        if (!groups.has(l.providerId)) groups.set(l.providerId, [])
        groups.get(l.providerId)!.push(l)
      }
      return [...groups.entries()]
        .map(([providerId, lives]) => ({
          providerId,
          provider: s.providers.find((p) => p.id === providerId),
          lives,
        }))
        .filter((g) => g.lives.length || g.provider)
    },
    templateUsage: (s) => (tid: string) => s.lives.filter((l) => l.templateId === tid).length,
    currentSession: (s) => s.sessions.find((x) => x.id === s.currentSessionId),
    /** 最后正在（或刚）进行的一轮——流式就写进它 */
    liveRound(s): RoundView | null {
      return s.rounds.length ? s.rounds[s.rounds.length - 1] : null
    },
  },

  actions: {
    async boot() {
      try {
        const health = await api.health()
        this.version = health.version
        this.dataVersion = health.dataVersion
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
        if (!this.currentSessionId && this.sessions.length) {
          this.currentSessionId = this.sessions[0].id
          this.syncSeatsFromSession()
          await this.loadRounds()
        }
      } catch (e) {
        this.problem = e instanceof Error ? e.message : String(e)
      }
    },

    go(where: 'round' | 'humans' | 'settings') {
      this.view = where
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

    openModal(kind: 'memory' | 'timeline' | 'life' | 'speaker', lifeId = '') {
      this.modal = kind
      this.modalLifeId = lifeId
      // 席位弹窗借用同一个参数传"第几号座位"（'0' = 新加一个）
      this.modalSeat = kind === 'speaker' ? Number(lifeId) || 0 : 0
    },
    closeModal() {
      this.modal = ''
      this.modalLifeId = ''
      this.modalSeat = 0
    },

    /** 把某位从桌上撤掉（座位号重排，免得出现空号）。 */
    async removeSeat(seatNo: number) {
      const next = this.seats.filter((s) => s.seatNo !== seatNo)
      await this.setSeats(next.map((s) => ({ lifeId: s.lifeId, model: s.model })))
    },

    /** 开一张桌子：有就用当前的，没有就建一张。 */
    async ensureSession(): Promise<Session> {
      const found = this.sessions.find((s) => s.id === this.currentSessionId)
      if (found) return found
      if (this.sessions.length) {
        this.currentSessionId = this.sessions[0].id
        this.syncSeatsFromSession()
        return this.sessions[0]
      }
      const created = await api.createSession({ title: '新的圆桌', mode: 'parallel', seats: [] })
      this.sessions = [created, ...this.sessions]
      this.currentSessionId = created.id
      this.syncSeatsFromSession()
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
          providerLabel: this.providerById(life?.providerId ?? '')?.label ?? '',
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

    clearRound() {
      this.rounds = []
    },

    /**
     * 把这张桌子上最近几轮读回来。
     *
     * 一轮的"原文"分散在**每位生命各自的记录**里（他记的是自己亲历的），
     * 所以这里按提问时间把它们拼回一轮一轮的样子——**桌子只记有谁，不记轮次**。
     */
    async loadRounds(limit = 3) {
      const session = this.currentSession
      if (!session?.seats.length) {
        this.rounds = []
        return
      }
      this.loading = true
      try {
        const perSeat: Array<{ seat: Seat; entries: any[] }> = []
        for (const seat of session.seats) {
          try {
            const got = await api.session(seat.lifeId, session.id, true)
            perSeat.push({ seat, entries: got.entries ?? [] })
          } catch {
            perSeat.push({ seat, entries: [] })
          }
        }
        // 按提问时间分组：每条 you 的开头就是一轮
        const byTime = new Map<number, RoundView>()
        for (const { entries } of perSeat) {
          for (const e of entries) {
            if (e.kind === 'you' && !byTime.has(e.t)) {
              byTime.set(e.t, { t: e.t, question: e.text ?? '', answers: [] })
            }
          }
        }
        const times = [...byTime.keys()].sort((a, b) => a - b)
        for (const { seat, entries } of perSeat) {
          const life = this.lifeById(seat.lifeId)
          for (const e of entries) {
            if (e.kind !== 'self') continue
            // 他的回答归到"最近一次提问"那一轮
            let t = times[0]
            for (const x of times) if (x <= e.t) t = x
            const rnd = byTime.get(t)
            if (!rnd) continue
            rnd.answers.push({
              seatNo: seat.seatNo,
              lifeId: seat.lifeId,
              name: life?.name ?? seat.lifeId,
              emoji: life?.emoji ?? '🙂',
              model: e.model || seat.model || '',
              text: e.text ?? '',
              reasoning: e.reasoning ?? '',
              notices: e.notices ?? [],
              error: '',
              running: false,
              ms: 0,
              tokens: e.tokens ?? 0,
              usage: {},
              finishReason: e.finishReason ?? '',
            })
          }
        }
        this.rounds = times
          .slice(-limit)
          .map((t) => byTime.get(t)!)
          .map((r) => ({ ...r, answers: r.answers.sort((a, b) => a.seatNo - b.seatNo) }))
      } finally {
        this.loading = false
      }
    },

    /** 问一句：先把这一轮的空位摆好（卡片立刻出现），再往里填字。 */
    async sendQuestion() {
      const q = this.question.trim()
      if (!q || this.roundRunning || !this.seats.length) return
      const session = await this.ensureSession()
      const rnd: RoundView = {
        t: Date.now(),
        question: q,
        answers: this.seats.map((s) => ({
          seatNo: s.seatNo,
          lifeId: s.lifeId,
          name: s.name,
          emoji: s.emoji,
          model: s.model,
          text: '',
          reasoning: '',
          notices: [],
          error: '',
          running: true,
          ms: 0,
          tokens: 0,
          usage: {},
          finishReason: '',
        })),
      }
      this.rounds = [...this.rounds, rnd]
      this.roundRunning = true
      this.question = ''
      const { done, stop } = ask({ sessionId: session.id, question: q }, (evt) => this.applyEvent(evt))
      this.stopRound = stop
      try {
        await done
      } finally {
        this.roundRunning = false
        this.stopRound = null
        for (const a of rnd.answers) {
          if (a.running) {
            a.running = false
            a.error = a.error || '这一轮没有正常收尾（可能是连接断了，或者你点了停）'
          }
        }
        this.refreshLives().catch(() => {})
        this.refreshSessions().catch(() => {})
      }
    },

    applyEvent(evt: StreamEvent) {
      const rnd = this.liveRound
      const ans = rnd?.answers.find((a) => a.seatNo === evt.seatNo)
      switch (evt.type) {
        case 'seat_start':
          if (ans) {
            ans.running = true
            ans.name = evt.name ?? ans.name
          }
          break
        case 'text':
          if (ans) ans.text += evt.text ?? ''
          break
        case 'reasoning':
          if (ans) ans.reasoning += evt.text ?? ''
          break
        case 'notice':
        case 'limit':
          if (ans && evt.text && !ans.notices.includes(evt.text)) ans.notices.push(evt.text)
          break
        case 'seat_done':
          if (ans) {
            ans.running = false
            ans.text = evt.content ?? ans.text
            ans.reasoning = evt.reasoning ?? ans.reasoning
            ans.error = evt.error ?? ''
            ans.ms = evt.ms ?? 0
            ans.tokens = evt.tokens ?? 0
            ans.usage = evt.usage ?? {}
            ans.finishReason = evt.finishReason ?? ''
            ans.window = evt.window
            ans.outputCap = evt.outputCap
            ans.keptEntries = evt.keptEntries
            ans.droppedEntries = evt.droppedEntries
            ans.model = evt.model ?? ans.model
            if (evt.notices) for (const n of evt.notices) if (!ans.notices.includes(n)) ans.notices.push(n)
          }
          break
        case 'error':
          if (ans) {
            ans.running = false
            ans.error = evt.message ?? '出错了'
          } else if (evt.message) {
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
