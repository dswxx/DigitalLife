/**
 * 跨页面状态与交互流程；组件中的表单草稿、展开状态不放进这里。
 * 服务端文件是持久化来源，seats / rounds 是当前页面的展示模型。
 * 阅读聊天链路：boot → loadRounds → sendQuestion → applyEvent。
 */

import { defineStore } from 'pinia'
import { api, ask, API_VERSION, type LifeSummary, type Provider, type ProviderPreset, type Seat, type Session, type StreamEvent, type Template } from './api'

/** 这次要问的一位。 */
export interface SeatConfig {
  seatNo: number
  lifeId: string
  name: string
  emoji: string
  model: string
}

/** 某位在特定轮次中的展示快照，不能随当前席位编辑而改变历史身份。 */
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
}

/** 一轮对话。 */
export interface RoundView {
  id: string
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
    providerPresets: [] as ProviderPreset[],
    templates: [] as Template[],
    lives: [] as LifeSummary[],
    sessions: [] as Session[],


    view: 'round' as 'round' | 'humans' | 'settings',
    currentSessionId: '' as string,
    question: '',
    seats: [] as SeatConfig[],
    rounds: [] as RoundView[],
    roundRunning: false,
    stopRound: null as null | (() => void),
    loading: false,
    hasMoreRounds: false,
    historyBefore: '',
    historyRequest: 0, // 单调请求序号：慢历史响应不能覆盖后来切换到的会话。
    // 单独持有接收中的轮次，即使用户切换会话，旧流仍更新它原来的对象。
    streamRound: null as RoundView | null,
    streamSessionId: '',

    modal: '' as '' | 'speaker',
    /** 加人弹窗编的是第几位（0 = 新加一位） */
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
    /** 一次加载概况，校验接口版本，再打开最近会话的历史。 */
    async boot() {
      try {
        const b = await api.bootstrap()
        if (b.apiVersion !== API_VERSION) throw new Error('接口版本已更新，请刷新页面')
        this.version = b.version
        this.dataVersion = b.dataVersion
        this.dataDir = b.dataDir
        this.providers = b.settings.providers
        this.providerPresets = b.providerPresets ?? []
        this.templates = b.templates
        this.lives = b.lives
        this.sessions = b.sessions
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

    openModal(kind: 'speaker', seat = '0') {
      this.modal = kind
      // 加人弹窗借用同一个参数传"第几位"（'0' = 新加一位）
      this.modalSeat = Number(seat) || 0
    },
    closeModal() {
      this.modal = ''
      this.modalSeat = 0
    },

    /** 把某位从桌上撤掉（座位号重排，免得出现空号）。 */
    async removeSeat(seatNo: number) {
      const next = this.seats.filter((s) => s.seatNo !== seatNo)
      await this.setSeats(next.map((s) => ({ lifeId: s.lifeId, model: s.model })))
    },

    /** 优先复用当前会话，其次已有首个会话；确实没有时才创建空会话。 */
    async ensureSession(): Promise<Session> {
      const found = this.sessions.find((s) => s.id === this.currentSessionId)
      if (found) return found
      if (this.sessions.length) {
        this.currentSessionId = this.sessions[0].id
        this.syncSeatsFromSession()
        return this.sessions[0]
      }
      const created = await api.createSession({ title: '新的对话', seats: [] })
      this.sessions = [created, ...this.sessions]
      this.currentSessionId = created.id
      this.syncSeatsFromSession()
      return created
    },

    syncSeatsFromSession() {
      // 当前实体信息只用于编辑区；旧回答的名字、模型来自各轮自己的快照。
      const s = this.currentSession
      this.seats = (s?.seats ?? []).map((seat) => {
        const life = this.lifeById(seat.lifeId)
        return {
          seatNo: seat.seatNo,
          lifeId: seat.lifeId,
          name: life?.name ?? seat.lifeId,
          emoji: life?.emoji ?? '🙂',
          model: seat.model || life?.model || '',
        }
      })
    },

    /** 保存成功后才替换席位；后端还会检查本轮是否仍在后台进行。 */
    async setSeats(next: Array<{ lifeId: string; model?: string }>) {
      if (this.roundRunning) throw new Error('等这一轮回答结束后再调整提问对象')
      const session = await this.ensureSession()
      const seats: Seat[] = next.map((x, i) => {
        const life = this.lifeById(x.lifeId)
        return {
          seatNo: i + 1,
          lifeId: x.lifeId,
          providerId: life?.providerId ?? '',
          model: x.model ?? life?.model ?? '',
        }
      })
      const updated = await api.updateSession(session.id, { seats })
      this.sessions = this.sessions.map((s) => (s.id === updated.id ? updated : s))
      this.syncSeatsFromSession()
    },

    clearRound() {
      this.rounds = []
      this.hasMoreRounds = false
      this.historyBefore = ''
    },

    /** 【核心】按轮次读取历史，丢弃过期响应，并保留当前接收中的流式轮次。 */
    async loadRounds(limit = 3, older = false) {
      const sessionId = this.currentSessionId
      if (!sessionId) {
        this.clearRound()
        return
      }
      const request = ++this.historyRequest
      this.loading = true
      try {
        const page = await api.sessionMessages(sessionId, limit, older ? this.historyBefore : '')
        // 请求发出后可能切换会话或再次翻页；旧响应不应覆盖新的展示状态。
        if (this.currentSessionId !== sessionId || request !== this.historyRequest) return
        const loaded: RoundView[] = page.rounds.map((r) => ({
          id: r.id, t: r.t, question: r.question,
          answers: r.answers.map((a) => ({
            seatNo: a.seatNo, lifeId: a.lifeId, name: a.name ?? a.lifeId,
            emoji: a.emoji ?? '🙂', model: a.model ?? '', text: a.content ?? '',
            reasoning: a.reasoning ?? '', notices: a.notices ?? [], error: a.error ?? '',
            running: false, ms: a.ms ?? 0, tokens: a.tokens ?? 0, usage: a.usage ?? {},
            finishReason: a.finishReason ?? '',
          })),
        }))
        if (older) {
          const existing = new Set(this.rounds.map((r) => r.id))
          this.rounds = [...loaded.filter((r) => !existing.has(r.id)), ...this.rounds]
        } else {
          this.rounds = loaded
          // 重新加载历史时保留同会话正在接收的对象，避免新到的流事件写入不可见旧对象。
          if (this.streamSessionId === sessionId && this.streamRound) {
            const live = this.streamRound
            this.rounds = this.rounds.filter((r) => r.id !== live.id)
            this.rounds.push(live)
          }
        }
        this.hasMoreRounds = page.hasMore
        this.historyBefore = page.before
      } catch (e) {
        if (this.currentSessionId === sessionId) this.problem = e instanceof Error ? e.message : String(e)
      } finally {
        if (this.currentSessionId === sessionId && request === this.historyRequest) this.loading = false
      }
    },

    /** 【核心】先建立本轮卡片，再发送一次请求，将交错事件写入固定轮次对象。 */
    async sendQuestion() {
      const q = this.question.trim()
      if (!q || this.roundRunning || !this.seats.length) return
      const session = await this.ensureSession()
      const rnd: RoundView = {
        id: '',
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
      const live = this.liveRound!
      this.streamRound = live
      this.streamSessionId = session.id
      this.roundRunning = true
      this.question = ''
      // 捕获 live，不在回调里重新取“当前最后一轮”，避免切换会话后写错位置。
      const { done, stop } = ask({ sessionId: session.id, question: q }, (evt) => this.applyEvent(evt, live))
      this.stopRound = stop
      try {
        await done
      } catch (e) {
        const message = e instanceof Error && e.name === 'AbortError'
          ? '已停止接收回答'
          : e instanceof Error ? e.message : String(e)
        for (const answer of live.answers) {
          if (answer.running) answer.error = message
        }
      } finally {
        // 连接结束不一定收到所有完成事件，仍在 running 的卡片必须明确显示未正常收尾。
        this.roundRunning = false
        this.streamRound = null
        this.streamSessionId = ''
        this.stopRound = null
        for (const a of live.answers) {
          if (a.running) {
            a.running = false
            a.error = a.error || '这一轮没有正常收尾（可能是连接断了，或者你点了停）'
          }
        }
        this.refreshLives().catch(() => {})
        this.refreshSessions().catch(() => {})
      }
    },

    /** 【核心】按 seatNo 定位回答；增量事件追加内容，完成事件用保存后的结果校准。 */
    applyEvent(evt: StreamEvent, rnd: RoundView | null) {
      const ans = rnd?.answers.find((a) => a.seatNo === evt.seatNo)
      switch (evt.type) {
        case 'round_start':
          // 本地先显示的临时轮次在这里获得服务端的正式 ID 和时间。
          if (rnd) { rnd.id = evt.roundId ?? rnd.id; rnd.t = evt.at ?? rnd.t }
          break
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
          // 完成事件包含完整回答，用最终结果替换增量拼接，避免中间状态残留。
          if (ans) {
            ans.running = false
            ans.text = evt.content ?? ans.text
            ans.reasoning = evt.reasoning ?? ans.reasoning
            ans.error = evt.error ?? ''
            ans.ms = evt.ms ?? 0
            ans.tokens = evt.tokens ?? 0
            ans.usage = evt.usage ?? {}
            ans.finishReason = evt.finishReason ?? ''
            ans.model = evt.model ?? ans.model
            if (evt.notices) for (const n of evt.notices) if (!ans.notices.includes(n)) ans.notices.push(n)
          }
          break
        case 'error':
          if (ans) {
            ans.running = false
            ans.error = evt.message ?? '出错了'
          } else if (evt.message) {
            // 无 seatNo 表示整轮错误，只影响尚未结束的回答，保留已完成的内容。
            for (const answer of rnd?.answers ?? []) {
              if (answer.running) {
                answer.error = evt.message
                answer.running = false
              }
            }
          }
          break
        default:
          break
      }
    },

    stop() {
      // 只取消当前 fetch；后台继续保存，下一次提问仍由服务端的整轮锁判断是否可用。
      this.stopRound?.()
    },
  },
})
