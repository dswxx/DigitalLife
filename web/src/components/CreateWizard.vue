<script setup lang="ts">
/**
 * 新建数字人：**两步，别的都不要。**
 *
 *   ① 人格（从模板挑 / 现场写）   ② 选一家 API（平台 → 模型）
 *
 * 编号只出现在这里——因为它真的有先后顺序：没有人格就没有"谁"，
 * 没有平台就没有"在哪活着"。其余地方一律不加编号。
 */
import { computed, ref, watch } from 'vue'
import { api, type Template } from '../api'
import { useApp } from '../store'

const emit = defineEmits<{ (e: 'close'): void; (e: 'created', lifeId: string): void }>()
const app = useApp()

const step = ref<1 | 2>(1)
const mode = ref<'template' | 'write'>('template')
const templateId = ref('')
const draft = ref({ name: '', emoji: '🙂', tagline: '', system: '' })
const providerId = ref('')
const model = ref('')
const models = ref<{ id: string }[]>([])
const fetching = ref(false)
const busy = ref(false)
const problem = ref('')

const picked = computed<Template | undefined>(() => app.templates.find((t) => t.id === templateId.value))

// 默认挑第一个模板；没有模板就先别让用户点进第二步
watch(
  () => app.templates,
  (list) => {
    if (!templateId.value && list.length) templateId.value = list[0].id
  },
  { immediate: true },
)

function pick(p: string) {
  providerId.value = p
  models.value = []
  model.value = app.providerById(p)?.models?.[0]?.id ?? ''
  void loadModels()
}

async function loadModels() {
  if (!providerId.value) return
  fetching.value = true
  problem.value = ''
  try {
    const got = await api.fetchModels(providerId.value)
    models.value = got.models
    if (!model.value && got.models.length) model.value = got.models[0].id
  } catch (e) {
    // 拉不到列表不是死路：让他手填模型名。**失败要看得见**。
    problem.value = (e instanceof Error ? e.message : String(e)) + '（可以在下面手动填模型名）'
  } finally {
    fetching.value = false
  }
}

async function create() {
  busy.value = true
  problem.value = ''
  try {
    let tid = templateId.value
    if (mode.value === 'write') {
      if (!draft.value.name.trim()) {
        problem.value = '给他起个名字'
        return
      }
      const tpl = await api.createTemplate({
        name: draft.value.name.trim(),
        emoji: draft.value.emoji || '🙂',
        tagline: draft.value.tagline,
        system: draft.value.system,
      })
      await app.refreshTemplates()
      tid = tpl.id
    }
    if (!tid) {
      problem.value = '先选一份人格'
      return
    }
    if (!providerId.value) {
      problem.value = '选一家平台，他要在那家平台上活着'
      return
    }
    if (!model.value.trim()) {
      problem.value = '选一个模型'
      return
    }
    const life = await api.createLife({
      templateId: tid,
      providerId: providerId.value,
      model: model.value.trim(),
      name: mode.value === 'write' ? draft.value.name.trim() : undefined,
      emoji: mode.value === 'write' ? draft.value.emoji : undefined,
    })
    await app.refreshLives()
    emit('created', life.id)
    emit('close')
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

const canNext = computed(() => (mode.value === 'template' ? !!templateId.value : !!draft.value.name.trim()))
</script>

<template>
  <div class="drawer-mask" @click.self="emit('close')">
    <div class="drawer" style="width: min(720px, 94vw)">
      <div class="drawer-head">
        <strong>新建数字人</strong>
        <div class="spacer"></div>
        <button class="btn sm ghost" @click="emit('close')">关闭</button>
      </div>

      <div class="drawer-body">
        <div class="steps">
          <div class="step" :class="{ on: step === 1 }">
            <span class="n">1</span><span>人格</span>
          </div>
          <span>→</span>
          <div class="step" :class="{ on: step === 2 }">
            <span class="n">2</span><span>选一家 API</span>
          </div>
        </div>

        <!-- ── 第一步：人格 ── -->
        <template v-if="step === 1">
          <div class="row" style="margin-bottom: 12px">
            <button class="tab" :class="{ on: mode === 'template' }" @click="mode = 'template'">从模板开始</button>
            <button class="tab" :class="{ on: mode === 'write' }" @click="mode = 'write'">现场写一份</button>
          </div>

          <template v-if="mode === 'template'">
            <p class="hint" style="margin-bottom: 10px">
              模板是蓝图（不绑厂商、没有记忆）。用它创建出来的是一个**个体**，从此刻起有自己的记忆。
            </p>
            <div class="tpl-grid">
              <button
                v-for="t in app.templates"
                :key="t.id"
                class="tpl"
                :style="t.id === templateId ? 'border-color: var(--accent); box-shadow: var(--shadow)' : ''"
                @click="templateId = t.id"
              >
                <div class="emoji">{{ t.emoji }}</div>
                <div class="name">
                  {{ t.name }}
                  <span v-if="t.bare" class="tag bare">裸模型</span>
                </div>
                <div class="tagline">{{ t.tagline || (t.bare ? '不发人格，看模型本来的样子' : '（没有一句话说明）') }}</div>
                <div class="count mono">{{ app.templateUsage(t.id) }} 个生命</div>
              </button>
            </div>
            <div v-if="picked" style="margin-top: 14px">
              <h3>{{ picked.emoji }} {{ picked.name }} 的人格</h3>
              <p v-if="picked.bare" class="note">这份模板是**纯基线**：请求里一个 system 字段都不发，连时间感和记忆都不注入。用他当对照组。</p>
              <textarea v-else :value="picked.system" rows="8" readonly style="font-family: var(--mono); font-size: 12.5px"></textarea>
              <p class="hint">改名或改内容会生成一份**新的**模板（id 由内容算出来），不会动到别人。</p>
            </div>
          </template>

          <template v-else>
            <div class="field"><label>名字</label><input v-model="draft.name" type="text" placeholder="例如：孙宇晨" /></div>
            <div class="field"><label>图标</label><input v-model="draft.emoji" type="text" style="width: 90px" /></div>
            <div class="field"><label>一句话</label><input v-model="draft.tagline" type="text" placeholder="会显示在库里的那张卡上" /></div>
            <div class="field" style="align-items: start">
              <label>系统提示词</label>
              <textarea v-model="draft.system" rows="10" placeholder="他是谁、怎么说话、在意什么…"></textarea>
            </div>
            <p class="hint">
              想让模型原样作答、不带任何人格？留空系统提示词，并把「裸模型」那份模板当成参照。
            </p>
          </template>
        </template>

        <!-- ── 第二步：平台与模型 ── -->
        <template v-else>
          <p class="hint" style="margin-bottom: 10px">
            他会在这家平台上活着、积累自己的记忆。同一份人格可以建很多个个体（比如各绑一家）。
          </p>
          <div v-if="!app.providers.length" class="empty">
            还没有配平台。去「设置」里加一家（填 Base URL 和 API Key）。
          </div>
          <div v-else class="row wrap" style="gap: 8px">
            <button
              v-for="p in app.providers"
              :key="p.id"
              class="btn"
              :class="{ primary: providerId === p.id }"
              @click="pick(p.id)"
            >
              {{ p.label || p.name }}
              <span class="mono" style="margin-left: 6px">{{ p.hasKey ? '已配密钥' : '没密钥' }}</span>
            </button>
          </div>

          <div v-if="providerId" style="margin-top: 16px">
            <div class="field">
              <label>模型</label>
              <div class="row">
                <select v-if="models.length" v-model="model" class="grow">
                  <option v-for="m in models" :key="m.id" :value="m.id">{{ m.id }}</option>
                </select>
                <input v-else v-model="model" type="text" placeholder="这家没给模型列表，手动填模型名" class="grow" />
                <button class="btn sm" :disabled="fetching" @click="loadModels">
                  {{ fetching ? '拉取中…' : '重新拉取' }}
                </button>
              </div>
            </div>
            <p v-if="problem" class="note" style="margin-top: 8px">{{ problem }}</p>
          </div>
        </template>

        <p v-if="problem && step === 1" class="note bad" style="margin-top: 12px">{{ problem }}</p>
      </div>

      <div class="row" style="padding: 12px 18px; border-top: 1px solid var(--line); background: var(--panel)">
        <button v-if="step === 2" class="btn ghost" @click="step = 1">← 上一步</button>
        <div class="spacer"></div>
        <button v-if="step === 1" class="btn primary" :disabled="!canNext" @click="step = 2">下一步：选一家 API</button>
        <button v-else class="btn primary" :disabled="busy" @click="create">
          {{ busy ? '创建中…' : '创建' }}
        </button>
      </div>
    </div>
  </div>
</template>
