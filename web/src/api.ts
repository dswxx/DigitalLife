/**
 * 接口客户端。**唯一碰令牌和 fetch 的地方。**
 *
 * 两件事值得说明：
 *
 * 1. **令牌从 window.__RT_BOOT__ 里拿。** 它是服务端每次启动随机生成的，
 *    注入在 index.html 里。所以前端产物可以随便拷，但没有令牌就调不通接口。
 *
 * 2. **流式是一次一行读的，不是等全部算完。** `fetch` + `ReadableStream` 手动切行，
 *    因为圆桌的整个观感就是"七个席位同时出字"——等整轮结束再渲染就没有那个感觉了。
 *    这里必须处理**跨块切断**：一行可能分两次到（`buffer` 就是为这个留的）。
 */

export interface Advanced {
  maxTokens: number
  historyRounds: number
  contextWindow: number
  requestTimeoutMs: number
}

export interface Provider {
  id: string
  name: string
  label?: string
  kind: 'openai' | 'anthropic'
  baseUrl: string
  apiKey: string
  hasKey?: boolean
  models?: { id: string; created?: number }[]
}

export interface Template {
  id: string
  name: string
  emoji: string
  tagline: string
  tags: string[]
  system: string
  bare: boolean
  builtin: boolean
  origin: string
  createdAt: number
}

export interface LifeSummary {
  id: string
  name: string
  emoji: string
  providerId: string
  model: string
  bornAt: number
  templateId: string
  templateName: string
  templateBare: boolean
  sessionCount: number
  memoryCount: number
  lastTalkedAt: number
}

export interface Memory {
  id: string
  name: string
  desc: string
  type: string
  scope: string
  at: number
  source: string
  text: string
  updated_at: number
}

export interface Seat {
  seatNo: number
  lifeId: string
  providerId: string
  model: string
  temperature: number | null
}

export interface Session {
  id: string
  title: string
  mode: 'parallel' | 'council'
  seats: Seat[]
  createdAt: number
  updatedAt: number
}

/** 流里推过来的事件。每块都带 seatNo——并排模式下多个席位是交错来的。 */
export interface StreamEvent {
  type:
    | 'seat_start' | 'text' | 'reasoning' | 'notice' | 'limit'
    | 'seat_done' | 'error' | 'round_done'
  seatNo?: number
  lifeId?: string
  name?: string
  text?: string
  message?: string
  content?: string
  reasoning?: string
  tokens?: number
  ms?: number
  finishReason?: string
  model?: string
  usage?: { input?: number; output?: number }
  notices?: string[]
  window?: number
  outputCap?: number
  keptEntries?: number
  droppedEntries?: number
  roundId?: string
  at?: number
}

declare global {
  interface Window {
    __RT_BOOT__?: { apiVersion: number; token: string; dataVersion: number }
  }
}

export const boot = window.__RT_BOOT__ ?? { apiVersion: 0, token: '', dataVersion: 0 }

/** 后端接口版本和我们对不上的时候，说人话让人刷新，而不是抛一堆解析错。 */
export const API_VERSION = 4

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      'content-type': 'application/json',
      'x-roundtable-token': boot.token,
      ...(init.headers ?? {}),
    },
  })
  const text = await res.text()
  let data: any = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    throw new ApiError(res.status, `后端返回的不是 JSON（${res.status}）：${text.slice(0, 200)}`)
  }
  if (!res.ok) throw new ApiError(res.status, data?.error ?? `请求失败（${res.status}）`)
  return data as T
}

const get = <T>(p: string) => request<T>(p)
const post = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: 'POST', body: JSON.stringify(body ?? {}) })
const patch = <T>(p: string, body: unknown) =>
  request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })
const del = <T>(p: string) => request<T>(p, { method: 'DELETE' })

export const api = {
  health: () => get<{ ok: boolean; version: string; apiVersion: number; dataVersion: number; dataDir: string }>('/api/health'),
  bootstrap: () => get<any>('/api/bootstrap'),

  getSettings: () => get<{ providers: Provider[]; advanced: Advanced }>('/api/settings'),
  saveSettings: (payload: { providers?: Provider[]; advanced?: Partial<Advanced> }) =>
    post<{ providers: Provider[]; advanced: Advanced }>('/api/settings', payload),
  fetchModels: (id: string) => post<{ models: { id: string }[] }>(`/api/providers/${id}/models`),
  testProvider: (id: string) =>
    post<{ ok: boolean; message: string; models: { id: string }[] }>(`/api/providers/${id}/test`),

  templates: () => get<Template[]>('/api/templates'),
  createTemplate: (t: Partial<Template>) => post<Template>('/api/templates', t),
  updateTemplate: (id: string, t: Partial<Template>) => patch<Template>(`/api/templates/${id}`, t),
  deleteTemplate: (id: string) => del<{ ok: boolean }>(`/api/templates/${id}`),

  lives: () => get<LifeSummary[]>('/api/lives'),
  life: (id: string) => get<any>(`/api/lives/${id}`),
  createLife: (payload: { templateId: string; providerId: string; model: string; name?: string; emoji?: string }) =>
    post<LifeSummary>('/api/lives', payload),
  updateLife: (id: string, payload: Record<string, unknown>) => patch<LifeSummary>(`/api/lives/${id}`, payload),
  deleteLife: (id: string) => del<{ ok: boolean }>(`/api/lives/${id}`),

  memories: (lifeId: string) => get<{ index: string; memories: Memory[]; types: string[] }>(`/api/lives/${lifeId}/memory`),
  addMemory: (lifeId: string, m: Partial<Memory>) => post<Memory>(`/api/lives/${lifeId}/memory`, m),
  editMemory: (lifeId: string, mid: string, m: Partial<Memory>) =>
    patch<Memory>(`/api/lives/${lifeId}/memory/${mid}`, m),
  deleteMemory: (lifeId: string, mid: string) => del<{ ok: boolean }>(`/api/lives/${lifeId}/memory/${mid}`),

  session: (lifeId: string, sid: string, tail = false) =>
    get<any>(`/api/lives/${lifeId}/sessions/${sid}${tail ? '?tail=1' : ''}`),
  timeline: (lifeId: string) => get<{ days: any[] }>(`/api/lives/${lifeId}/timeline`),
  compact: (lifeId: string, sid: string) =>
    post<any>(`/api/lives/${lifeId}/sessions/${sid}/compact`, {}),

  sessions: () => get<Session[]>('/api/sessions'),
  createSession: (payload: Partial<Session>) => post<Session>('/api/sessions', payload),
  updateSession: (id: string, payload: Partial<Session>) => patch<Session>(`/api/sessions/${id}`, payload),
  deleteSession: (id: string) => del<{ ok: boolean }>(`/api/sessions/${id}`),
}

/**
 * 问一句，边收边把事件交给 `onEvent`。
 *
 * 返回一个函数，调用它可以中途停掉这一轮（用户点了「停」）。
 */
export function ask(
  payload: { sessionId: string; question: string; seatNo?: number },
  onEvent: (evt: StreamEvent) => void,
): { done: Promise<void>; stop: () => void } {
  const ctl = new AbortController()
  const done = (async () => {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-roundtable-token': boot.token },
      body: JSON.stringify(payload),
      signal: ctl.signal,
    })
    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => '')
      onEvent({ type: 'error', message: text || `请求失败（${res.status}）` })
      return
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done: finished, value } = await reader.read()
      if (finished) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      // 一行一个事件。**跨块切断是常态**，所以留了 buffer。
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim()
        buf = buf.slice(idx + 1)
        if (line) {
          try {
            onEvent(JSON.parse(line) as StreamEvent)
          } catch {
            // 半行不该发生（我们按行切），真发生了就跳过，不中断整轮
          }
        }
      }
    }
    const tail = buf.trim()
    if (tail) {
      try {
        onEvent(JSON.parse(tail) as StreamEvent)
      } catch {
        /* 同上 */
      }
    }
  })()
  return { done, stop: () => ctl.abort() }
}
