<script setup lang="ts">
/** 管理平台、密钥和模型列表；提交密钥掩码时由后端保留真实值。 */
import { computed, ref } from 'vue'
import { api, API_VERSION, type Provider, type ProviderPreset } from '../api'
import { useApp } from '../store'

const app = useApp()
const apiVersion = API_VERSION
const busy = ref(false)
const problem = ref('')
const ok = ref('')
const testing = ref('')
const testResult = ref<Record<string, string>>({})

/** 正在编辑的那一家。`null` = 没打开弹窗。 */
const draft = ref<Provider | null>(null)
const modelsText = ref('')
/** 这次编辑是从哪个模板来的（新加时用来显示说明和申请 Key 的链接） */
const draftPreset = ref<ProviderPreset | null>(null)
const isNew = computed(() => !!draft.value && !app.providers.some((p) => p.id === draft.value!.id))
/** 还没存到服务器上就没有 id 可测 */
const canTest = computed(() => !!draft.value && !isNew.value)

function ago(ms: number): string {
  if (!ms) return ''
  const diff = Date.now() - ms
  if (diff < 90_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  const d = new Date(ms)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

function host(url: string): string {
  try {
    return new URL(url).host.toLowerCase()
  } catch {
    return (url || '').toLowerCase().replace(/^https?:\/\//, '').split('/')[0]
  }
}

/** 这一格是不是已经加过了（按 presetKey 认，老数据没有 presetKey 就按域名认）。 */
function has(preset: ProviderPreset): boolean {
  return app.providers.some(
    (p) => p.presetKey === preset.key || (!!preset.baseUrl && host(p.baseUrl) === host(preset.baseUrl)),
  )
}

function presetFor(p: Provider): ProviderPreset | null {
  if (p.presetKey) {
    const got = app.providerPresets.find((x) => x.key === p.presetKey)
    if (got) return got
  }
  return app.providerPresets.find((x) => !!x.baseUrl && host(p.baseUrl) === host(x.baseUrl)) ?? null
}

function edit(p: Provider) {
  draft.value = { ...p, models: [...(p.models ?? [])] }
  modelsText.value = (p.models ?? []).map((m) => m.id).join('\n')
  draftPreset.value = presetFor(p)
}

function addFrom(preset: ProviderPreset) {
  draft.value = {
    id: `pv_${Math.random().toString(36).slice(2, 10)}`,
    name: preset.label,
    label: preset.label,
    kind: preset.kind,
    baseUrl: preset.baseUrl,
    apiKey: '',
    hasKey: false,
    models: preset.models.map((id) => ({ id })),
    presetKey: preset.key,
  }
  modelsText.value = preset.models.join('\n')
  draftPreset.value = preset
}

async function saveDraft() {
  const d = draft.value
  if (!d) return
  problem.value = ''
  ok.value = ''
  if (!d.label?.trim()) {
    problem.value = '给这个平台起个名字'
    return
  }
  if (!d.baseUrl?.trim()) {
    problem.value = 'Base URL 不能空'
    return
  }
  if (isNew.value && !d.apiKey?.trim()) {
    problem.value = '填一下 API Key（它只保存在你自己电脑上）'
    return
  }
  busy.value = true
  try {
    d.name = d.label.trim()
    d.label = d.label.trim()
    d.baseUrl = d.baseUrl.trim()
    d.models = modelsText.value
      .split('\n')
      .map((x) => x.trim())
      .filter(Boolean)
      .map((id) => ({ id }))
    const others = app.providers.filter((p) => p.id !== d.id)
    const saved = await api.saveSettings({ providers: [...others, d] })
    app.providers = saved.providers
    ok.value = isNew.value ? `加好了：${d.label}` : '存好了。'
    draft.value = null
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function removeDraft() {
  const d = draft.value
  if (!d) return
  busy.value = true
  try {
    const saved = await api.saveSettings({ providers: app.providers.filter((p) => p.id !== d.id) })
    app.providers = saved.providers
    ok.value = `删掉了：${d.label}`
    draft.value = null
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function test(p: Provider) {
  testing.value = p.id
  testResult.value[p.id] = ''
  try {
    const got = await api.testProvider(p.id)
    testResult.value[p.id] = (got.ok ? '✅ ' : '❌ ') + got.message
  } catch (e) {
    testResult.value[p.id] = '❌ ' + (e instanceof Error ? e.message : String(e))
  } finally {
    testing.value = ''
  }
}

async function pull(p: Provider) {
  testing.value = p.id
  try {
    const got = await api.fetchModels(p.id)
    p.models = got.models
    p.modelsFetchedAt = got.modelsFetchedAt
    if (draft.value?.id === p.id) modelsText.value = got.models.map((m) => m.id).join('\n')
    testResult.value[p.id] = `✅ 拉到 ${got.models.length} 个模型`
  } catch (e) {
    testResult.value[p.id] = '❌ ' + (e instanceof Error ? e.message : String(e))
  } finally {
    testing.value = ''
  }
}
</script>

<template>
  <div class="stage-scroll">
    <div class="page">
      <div class="page-head">
        <h1>模型与密钥</h1>
        <p>
          这里的每一个平台，都会成为「一问多答」里一个可以当"大脑"的引擎。
          密钥只存在你自己的电脑上。
        </p>
      </div>

      <div v-if="problem" class="answer-note err">✕ {{ problem }}</div>
      <div v-if="ok" class="answer-note">✅ {{ ok }}</div>

      <!-- ── 已添加的平台 ── -->
      <template v-if="app.providers.length">
        <div class="side-label" style="padding: 0 0 8px">已添加的平台</div>
        <div v-for="p in app.providers" :key="p.id" class="provider-card">
          <div class="pr-head">
            <span class="pr-name">{{ p.label || p.name || '（未命名）' }}</span>
            <span class="pill">{{ p.kind === 'anthropic' ? 'Anthropic' : 'OpenAI 兼容' }}</span>
            <span v-if="p.hasKey" class="pill ok">🔒 {{ p.apiKey }}</span>
            <span v-else class="pill err">未配密钥</span>
            <span class="pill">{{ p.models?.length || 0 }} 个模型</span>
            <div style="flex: 1"></div>
            <span v-if="testResult[p.id]" class="mini-note">{{ testResult[p.id] }}</span>
            <button class="btn tiny" @click="edit(p)">编辑</button>
          </div>
          <div class="hint" style="margin-top: 8px">
            <span class="mono-sm">{{ p.baseUrl }}</span>
            <span v-if="p.modelsFetchedAt">　·　模型列表 {{ ago(p.modelsFetchedAt) }}拉过</span>
            <span v-else>　·　还没拉取过模型列表</span>
          </div>
        </div>
      </template>
      <div v-else class="card" style="margin-bottom: 20px">
        <div style="color: var(--text-2)">
          还没有添加任何平台。点下面的卡片开始，推荐先加 DeepSeek。
        </div>
      </div>

      <!-- ── 一键添加 ── -->
      <div class="side-label" style="padding: 14px 0 0">一键添加平台模板</div>
      <div class="preset-grid">
        <button
          v-for="preset in app.providerPresets"
          :key="preset.key"
          class="preset-tile"
          :style="has(preset) ? { opacity: '0.5' } : {}"
          @click="addFrom(preset)"
        >
          <div class="pt-name">{{ has(preset) ? '✓ ' : '＋ ' }}{{ preset.label }}</div>
          <div class="pt-note">{{ preset.note || preset.baseUrl || '自定义接口' }}</div>
        </button>
      </div>

      <!-- ── 数据在哪 ── -->
      <div class="card" style="margin-top: 28px; background: var(--bg)">
        <div class="side-label" style="padding: 0 0 10px">你的数据在哪</div>
        <div class="hint">
          全部数据都在程序目录的 <span class="mono-sm">{{ app.dataDir }}</span> 里，纯 JSON，
          可以随时拷贝走或手动修改：
        </div>
        <div class="hint" style="margin-top: 8px">
          · <span class="mono-sm">data/settings.json</span> —— 平台与密钥
        </div>
        <div class="hint">
          · <span class="mono-sm">data/templates/*.json</span> —— 人格蓝图
        </div>
        <div class="hint">
          · <span class="mono-sm">data/sessions/&lt;会话&gt;/</span> —— 会话概况与完整消息
        </div>
        <div class="hint">
          · <span class="mono-sm">data/humans/&lt;生命&gt;/history.jsonl</span> —— 从创建起的统一亲历记录
        </div>
        <div class="hint" style="margin-top: 8px">
          版本 <b>{{ app.version }}</b> · 数据格式 v{{ app.dataVersion }} · 接口 v{{ apiVersion }}
        </div>
      </div>
    </div>

    <!-- ── 添加 / 编辑一家平台 ── -->
    <div v-if="draft" class="modal-mask" @click.self="draft = null">
      <div class="modal wide">
        <div class="modal-head">
          <h3>{{ isNew ? '添加平台' : `编辑「${draft.label || '平台'}」` }}</h3>
          <div style="flex: 1"></div>
          <button class="btn tiny" @click="draft = null">关闭</button>
        </div>

        <div class="modal-body">
          <div class="row">
            <div class="field" style="flex: 1">
              <label>显示名称</label>
              <input v-model="draft.label" type="text" placeholder="例如：DeepSeek" />
            </div>
            <div class="field" style="width: 210px">
              <label>接口协议</label>
              <select v-model="draft.kind">
                <option value="openai">OpenAI 兼容</option>
                <option value="anthropic">Anthropic</option>
              </select>
            </div>
          </div>

          <div class="field">
            <label>Base URL</label>
            <input v-model="draft.baseUrl" type="text" placeholder="https://api.deepseek.com/v1" />
            <div class="hint">OpenAI 兼容接口通常以 /v1 结尾；只填域名的话会自动补 /v1。</div>
          </div>

          <div class="field">
            <label>API Key</label>
            <input
              v-model="draft.apiKey"
              type="password"
              :placeholder="draft.hasKey ? '已保存（留空就是不改）' : 'sk-…'"
            />
            <div class="hint">🔒 密钥只保存在你自己电脑的 data/settings.json 里，不会上传到任何地方。</div>
          </div>

          <div class="field">
            <label>模型列表</label>
            <textarea v-model="modelsText" rows="5" placeholder="每行一个模型名"></textarea>
            <div class="hint">每行一个。加人那里从这里下拉选。</div>
          </div>

          <div class="row wrap">
            <button class="btn" :disabled="!canTest || testing === draft.id" @click="test(draft)">
              {{ testing === draft.id ? '测着呢…' : '测一下' }}
            </button>
            <button class="btn" :disabled="!canTest || testing === draft.id" @click="pull(draft)">
              拉模型列表
            </button>
            <span v-if="!canTest" class="composer-hint">先点「保存」，再测/拉</span>
            <span v-if="testResult[draft.id]" class="mini-note">{{ testResult[draft.id] }}</span>
          </div>

          <div v-if="draftPreset?.consoleUrl" class="hint" style="margin-top: 10px">
            申请 Key：
            <a :href="draftPreset.consoleUrl" target="_blank" rel="noopener noreferrer">
              {{ draftPreset.consoleUrl }}
            </a>
          </div>
          <div v-if="draftPreset?.note" class="hint">💡 {{ draftPreset.note }}</div>

          <div v-if="!isNew" style="margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--border-soft)">
            <button class="btn danger" :disabled="busy" @click="removeDraft">删除这个平台</button>
          </div>
        </div>

        <div class="modal-foot">
          <div style="flex: 1"></div>
          <button class="btn" @click="draft = null">取消</button>
          <button class="btn primary" :disabled="busy" @click="saveDraft">
            {{ busy ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
