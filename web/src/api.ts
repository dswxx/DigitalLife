/** 类型化 HTTP 客户端与 NDJSON 流解析。令牌和 fetch 统一放在这里。 */

export interface Provider {
  id: string
  name: string
  label?: string
  kind: 'openai' | 'anthropic'
  baseUrl: string
  apiKey: string
  hasKey?: boolean
  models?: { id: string; created?: number }[]
  /** 上次成功拉模型列表的时间（设置页显示「模型列表 X 前拉过」） */
  modelsFetchedAt?: number
  /** 从哪个平台模板加进来的（那排方块用它打 ✓） */
  presetKey?: string | null
}

/** 「一键添加平台模板」那一格：数据在 `presets.json` 里，前端只负责画。 */
export interface ProviderPreset {
  key: string
  label: string
  kind: 'openai' | 'anthropic'
  baseUrl: string
  models: string[]
  note?: string
  consoleUrl?: string
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
  lastTalkedAt: number
}

export interface Seat {
  seatNo: number
  lifeId: string
  providerId: string
  model: string
}

export interface Session {
  id: string
  title: string
  seats: Seat[]
  createdAt: number
  updatedAt: number
  rounds?: number
  seatCount?: number
}

export interface Bootstrap {
  version: string
  apiVersion: number
  dataVersion: number
  dataDir: string
  settings: { providers: Provider[] }
  providerPresets: ProviderPreset[]
  templates: Template[]
  lives: LifeSummary[]
  sessions: Session[]
}

/** 流里推过来的事件。每块都带 seatNo——几位同时答，字是交错来的。 */
export interface StreamEvent {
  type:
    | 'seat_start' | 'text' | 'reasoning' | 'notice' | 'limit'
    | 'seat_done' | 'error' | 'round_start' | 'round_done'
  seatNo?: number
  lifeId?: string
  name?: string
  text?: string
  message?: string
  error?: string
  content?: string
  reasoning?: string
  tokens?: number
  ms?: number
  finishReason?: string
  model?: string
  usage?: { input?: number; output?: number }
  notices?: string[]
  roundId?: string
  at?: number
}

declare global {
  interface Window {
    __DL_BOOT__?: { apiVersion: number; token: string; dataVersion: number }
  }
}

export const boot = window.__DL_BOOT__ ?? { apiVersion: 0, token: '', dataVersion: 0 }

/** 后端接口版本和我们对不上的时候，说人话让人刷新，而不是抛一堆解析错。 */
export const API_VERSION = 7

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
      'x-digitallife-token': boot.token,
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
  bootstrap: () => get<Bootstrap>('/api/bootstrap'),

  saveSettings: (payload: { providers?: Provider[] }) =>
    post<{ providers: Provider[] }>('/api/settings', payload),
  fetchModels: (id: string) =>
    post<{ models: { id: string }[]; modelsFetchedAt: number }>(`/api/providers/${id}/models`),
  testProvider: (id: string) =>
    post<{ ok: boolean; message: string; models: { id: string }[] }>(`/api/providers/${id}/test`),

  templates: () => get<Template[]>('/api/templates'),
  createTemplate: (t: Partial<Template>) => post<Template>('/api/templates', t),
  updateTemplate: (id: string, t: Partial<Template>) => patch<Template>(`/api/templates/${id}`, t),
  deleteTemplate: (id: string) => del<{ ok: boolean }>(`/api/templates/${id}`),

  lives: () => get<LifeSummary[]>('/api/lives'),
  createLife: (payload: { templateId: string; providerId: string; model: string; name?: string; emoji?: string }) =>
    post<LifeSummary>('/api/lives', payload),
  deleteLife: (id: string) => del<{ ok: boolean }>(`/api/lives/${id}`),

  sessions: () => get<Session[]>('/api/sessions'),
  sessionMessages: (id: string, limit = 3, before = '') =>
    get<{ rounds: Array<{ id: string; t: number; question: string; answers: Array<Omit<StreamEvent, 'type'> & { seatNo: number; lifeId: string; emoji?: string }> }>; hasMore: boolean; before: string }>(
      `/api/sessions/${id}/messages?limit=${limit}&before=${encodeURIComponent(before)}`,
    ),
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
  payload: { sessionId: string; question: string },
  onEvent: (evt: StreamEvent) => void,
): { done: Promise<void>; stop: () => void } {
  const ctl = new AbortController()
  const done = (async () => {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-digitallife-token': boot.token },
      body: JSON.stringify(payload),
      signal: ctl.signal,
    })
    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => '')
      let message = text || `请求失败（${res.status}）`
      try { message = JSON.parse(text).error || message } catch { /* 普通文本错误 */ }
      throw new ApiError(res.status, message)
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
          onEvent(JSON.parse(line) as StreamEvent)
        }
      }
    }
    const tail = (buf + decoder.decode()).trim()
    if (tail) {
      onEvent(JSON.parse(tail) as StreamEvent)
    }
  })()
  return { done, stop: () => ctl.abort() }
}
