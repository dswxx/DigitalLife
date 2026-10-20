/** 类型化 HTTP 客户端与 NDJSON 流解析。令牌和 fetch 统一放在这里。 */

/** 平台配置：读取时 apiKey 为掩码；编辑提交时也可包含用户刚输入的新密钥。 */
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

/** 可复用人格；bare 表示普通 AI，请求时省略人格并按会话隔离历史。 */
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

/** 列表只使用轻量概况，完整个人历史由服务端按需加载。 */
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

/** 当前会话的参与配置；lifeId 是持续身份，seatNo 是这一轮流事件的卡片编号。 */
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

/** 参与者事件携带 seatNo；轮次事件和整轮错误可以不带，前端按 type 分派。 */
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

// 后端在入口 HTML 中注入令牌；缺少注入时不伪造凭据，接口会明确拒绝请求。
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
  // 普通 JSON 接口共用错误解析；聊天流在 ask 中单独逐行消费，不能整体 res.json()。
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
 * 【核心】只发起一条后端请求，持续接收由多个模型事件汇合的 NDJSON 响应。
 * done 表示接收结束；stop 取消浏览器接收，后台模型调用与保存仍继续。
 * 网络块不等于消息边界：TextDecoder 保留半个 UTF-8 字符，buf 保留半行 JSON。
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
      // 一次读取可能是半行或多行；完整行立即分派，余下内容留给下次读取。
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim()
        buf = buf.slice(idx + 1)
        if (line) {
          onEvent(JSON.parse(line) as StreamEvent)
        }
      }
    }
    // 结束时冲刷解码器，并兼容最后一个事件没有尾换行的响应。
    const tail = (buf + decoder.decode()).trim()
    if (tail) {
      onEvent(JSON.parse(tail) as StreamEvent)
    }
  })()
  return { done, stop: () => ctl.abort() }
}
