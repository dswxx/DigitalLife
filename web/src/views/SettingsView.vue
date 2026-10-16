<script setup lang="ts">
/**
 * 模型与密钥。**骨架照老界面**：`.page-head` + 一家一个方块 + 高级设置。
 *
 * 两条从老项目继承的规矩在这一页落地：
 *   · **密钥只回掩码**：拿到的是 `sk-a…1234`，原样回传时后端会认出那是掩码、
 *     保留真密钥。所以只要不主动改那个框，密钥就不会被写坏。
 *   · **不静默改用户的设置**：保存后后端如果纠正了什么，明确显示出来。
 */
import { onMounted, ref } from 'vue'
import { api, API_VERSION, type Provider } from '../api'
import { useApp } from '../store'

const app = useApp()
const apiVersion = API_VERSION
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
    if (!p.label) p.label = '未命名平台'
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
          密钥只存在本机（<span class="mono-sm">{{ app.dataDir }}</span>），接口只回掩码——
          截图、录屏、贴给别人看都不会漏。数字人借助这里的平台活着。
        </p>
        <p class="composer-hint">
          版本 <b>{{ app.version }}</b> · 数据格式 v{{ app.dataVersion }} · 接口 v{{ apiVersion }}
        </p>
      </div>

      <div v-if="problem" class="answer-note err">✕ {{ problem }}</div>
      <div v-if="ok" class="answer-note">✅ {{ ok }}</div>

      <div v-for="(p, i) in app.providers" :key="p.id" class="category-block">
        <div class="category-head">
          <span class="category-icon">🔌</span>
          <span class="category-name">{{ p.label || p.name || '（未命名）' }}</span>
          <span class="pill">{{ p.kind === 'anthropic' ? 'Anthropic 协议' : 'OpenAI 兼容' }}</span>
          <span v-if="p.hasKey" class="pill ok">已配密钥</span>
          <span v-else class="pill warn">没填密钥</span>
          <div style="flex: 1"></div>
          <span v-if="testResult[p.id]" class="mini-note">{{ testResult[p.id] }}</span>
          <button v-if="confirmDel !== p.id" class="btn tiny" @click="confirmDel = p.id">移出</button>
          <button v-else class="btn tiny danger" @click="removeProvider(i); confirmDel = ''">确认移出</button>
        </div>
        <div class="card-body">
          <div class="row" style="gap: 10px">
            <div class="field" style="flex: 1">
              <label>显示名</label>
              <input v-model="p.label" type="text" placeholder="例如：DeepSeek 深度求索" />
            </div>
            <div class="field" style="width: 280px">
              <label>协议</label>
              <select v-model="p.kind">
                <option value="openai">OpenAI 兼容（DeepSeek / Kimi / 通义 / Ollama…）</option>
                <option value="anthropic">Anthropic（Claude）</option>
              </select>
            </div>
          </div>
          <div class="field">
            <label>Base URL</label>
            <input
              v-model="p.baseUrl"
              type="text"
              :placeholder="p.kind === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.deepseek.com'"
            />
          </div>
          <div class="field">
            <label>API Key</label>
            <input
              v-model="p.apiKey"
              type="password"
              :placeholder="p.hasKey ? '已保存（留空就是不改）' : 'sk-…'"
            />
          </div>
          <div class="row">
            <button class="btn" :disabled="testing === p.id" @click="test(p)">
              {{ testing === p.id ? '测试中…' : '测一下' }}
            </button>
            <button class="btn" :disabled="testing === p.id" @click="fetchModels(p)">拉模型列表</button>
            <span v-if="p.models?.length" class="mini-note">{{ p.models.length }} 个模型</span>
          </div>
        </div>
      </div>

      <button class="btn" style="margin-bottom: 24px" @click="addProvider">＋ 加一家平台</button>

      <div class="category-block">
        <div class="category-head">
          <span class="category-icon">⚙️</span>
          <span class="category-name">高级设置</span>
          <span class="pill">一般不用动</span>
        </div>
        <div class="card-body">
          <div class="field">
            <label>单次回答最长 token</label>
            <input v-model.number="advanced.maxTokens" type="number" min="1" max="32768" />
            <span class="hint">
              这是上限不是目标：模型答完就停。推理模型把思考也算在这里，
              所以太小会出现「思考说完、正文没轮到」。
            </span>
          </div>
          <div class="field">
            <label>上下文窗口</label>
            <input v-model.number="advanced.contextWindow" type="number" min="0" />
            <span class="hint">
              填 0 = 用平台报的值；平台没报就保守按 128k 估。宁可保守：
              猜大了直接 400，猜小了只是少带几轮。
            </span>
          </div>
          <div class="field">
            <label>最多带几轮</label>
            <input v-model.number="advanced.historyRounds" type="number" min="1" />
            <span class="hint">只是防极端的上限；真正的取舍按 token 预算算。</span>
          </div>
        </div>
      </div>

      <div class="row">
        <div style="flex: 1"></div>
        <button class="btn primary" :disabled="busy" @click="save">{{ busy ? '保存中…' : '保存' }}</button>
      </div>
    </div>
  </div>
</template>
