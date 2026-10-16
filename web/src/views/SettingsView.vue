<script setup lang="ts">
/**
 * 设置：平台（Base URL / 密钥 / 模型）和高级设置。
 *
 * 两条从老项目继承的规矩在这里落地：
 *   · **密钥只回掩码**。所以前端拿到的是 `sk-a…1234`，它原样回传时后端会认出
 *     那是掩码、保留真密钥。这一页只要不主动改那个框，密钥就不会被写坏。
 *   · **不静默改用户的设置**。保存后如果后端纠正了什么（比如输出上限超出范围），
 *     会在这里明确显示出来。
 */
import { onMounted, ref } from 'vue'
import { api, type Provider } from '../api'
import { useApp } from '../store'

const app = useApp()
const busy = ref(false)
const problem = ref('')
const ok = ref('')
const testing = ref('')
const testResult = ref<Record<string, string>>({})
const confirmDel = ref('')

const advanced = ref({ maxTokens: 8192, historyRounds: 50, contextWindow: 0, requestTimeoutMs: 300000 })

onMounted(() => {
  advanced.value = { ...advanced.value, ...app.advanced }
})

function blank(): Provider {
  return {
    id: `pv_${Math.random().toString(36).slice(2, 10)}`,
    name: '',
    label: '',
    kind: 'openai',
    baseUrl: '',
    apiKey: '',
    hasKey: false,
    models: [],
  }
}

function addProvider() {
  app.providers = [...app.providers, blank()]
}

function removeProvider(i: number) {
  app.providers = app.providers.filter((_, k) => k !== i)
}

async function save() {
  busy.value = true
  problem.value = ''
  ok.value = ''
  try {
    const saved = await api.saveSettings({ providers: app.providers, advanced: advanced.value })
    app.providers = saved.providers
    app.advanced = saved.advanced
    ok.value = '存好了。'
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

async function fetchModels(p: Provider) {
  testing.value = p.id
  try {
    const got = await api.fetchModels(p.id)
    p.models = got.models
    testResult.value[p.id] = `✅ 拉到 ${got.models.length} 个模型（记得保存）`
    if (!p.name) p.name = '未命名平台'
  } catch (e) {
    testResult.value[p.id] = '❌ ' + (e instanceof Error ? e.message : String(e))
  } finally {
    testing.value = ''
  }
}
</script>

<template>
  <h1>设置</h1>
  <p class="hint">密钥只存在本机（<span class="mono">{{ app.dataDir }}</span>），接口只回掩码。</p>

  <p v-if="problem" class="note bad" style="margin-top: 12px">{{ problem }}</p>
  <p v-if="ok" class="note good" style="margin-top: 12px">{{ ok }}</p>

  <h2>平台</h2>
  <div v-if="!app.providers.length" class="empty">还没有平台。点下面「＋ 加一家」。</div>

  <div v-for="(p, i) in app.providers" :key="p.id" class="card pad" style="margin-bottom: 12px">
    <div class="row" style="margin-bottom: 10px">
      <strong>{{ p.label || p.name || '（未命名）' }}</strong>
      <span class="tag" :class="{ bare: p.kind === 'anthropic' }">{{ p.kind === 'anthropic' ? 'Anthropic 协议' : 'OpenAI 兼容' }}</span>
      <span v-if="p.hasKey" class="tag">已配密钥</span>
      <span v-else class="tag" style="color: var(--warn-ink)">没填密钥</span>
      <div class="spacer"></div>
      <template v-if="confirmDel !== p.id">
        <button class="btn sm ghost" @click="confirmDel = p.id">移出</button>
      </template>
      <template v-else>
        <button class="btn sm danger" @click="removeProvider(i); confirmDel = ''">真的移出</button>
        <button class="btn sm ghost" @click="confirmDel = ''">算了</button>
      </template>
    </div>

    <div class="grid2">
      <div class="field"><label>显示名</label><input v-model="p.label" type="text" placeholder="例如：DeepSeek 深度求索" /></div>
      <div class="field">
        <label>协议</label>
        <select v-model="p.kind">
          <option value="openai">OpenAI 兼容（DeepSeek / Kimi / 通义 / Ollama…）</option>
          <option value="anthropic">Anthropic（Claude）</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label>Base URL</label>
      <input v-model="p.baseUrl" type="text" :placeholder="p.kind === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.deepseek.com'" />
    </div>
    <div class="field">
      <label>API Key</label>
      <input v-model="p.apiKey" type="password" :placeholder="p.hasKey ? '已保存（留空就是不改）' : 'sk-…'" />
    </div>

    <div class="row">
      <button class="btn sm" :disabled="testing === p.id" @click="test(p)">{{ testing === p.id ? '测试中…' : '测一下' }}</button>
      <button class="btn sm" :disabled="testing === p.id" @click="fetchModels(p)">拉模型列表</button>
      <span v-if="p.models?.length" class="mono">{{ p.models.length }} 个模型</span>
      <div class="spacer"></div>
      <span v-if="testResult[p.id]" class="hint">{{ testResult[p.id] }}</span>
    </div>
  </div>

  <button class="btn" @click="addProvider">＋ 加一家</button>

  <h2>高级设置</h2>
  <div class="card pad">
    <div class="field">
      <label>单次回答最长 token</label>
      <input v-model.number="advanced.maxTokens" type="number" min="1" max="32768" />
    </div>
    <p class="hint" style="margin: -4px 0 10px">
      这是**上限不是目标**：模型答完就停。推理模型把思考也算在这里，所以太小会出现"思考说完正文没轮到"。
    </p>
    <div class="field">
      <label>上下文窗口</label>
      <input v-model.number="advanced.contextWindow" type="number" min="0" />
    </div>
    <p class="hint" style="margin: -4px 0 10px">
      填 0 = 用平台报的值；平台没报就保守按 128k 估。**宁可保守**：猜大了直接 400，猜小了只是少带几轮。
    </p>
    <div class="field">
      <label>最多带几轮</label>
      <input v-model.number="advanced.historyRounds" type="number" min="1" />
    </div>
    <p class="hint" style="margin: -4px 0 0">这只是防极端的上限；真正的取舍是按 token 预算算的。</p>
  </div>

  <div class="row" style="margin-top: 18px">
    <div class="spacer"></div>
    <button class="btn primary" :disabled="busy" @click="save">{{ busy ? '保存中…' : '保存' }}</button>
  </div>
</template>
