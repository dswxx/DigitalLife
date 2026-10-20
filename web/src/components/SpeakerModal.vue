<script setup lang="ts">
/** 添加和编辑提问对象：按平台选人，可覆盖本次使用的模型。 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'

const app = useApp()

const isNew = computed(() => !app.modalSeat)
/** 正在编辑的那一位的座位号 */
const seatNo = computed(() => app.modalSeat)
/** 打开时那一位（编辑时预选他） */
const editing = computed(() => app.seats.find((s) => s.seatNo === app.modalSeat) ?? null)

const cat = ref('')
const lifeId = ref('')
const model = ref('')
const fetching = ref(false)
const problem = ref('')

function available(id: string): boolean {
  // 同一会话不能重复加入同一个实体；编辑当前席位时允许保留原来的实体。
  return !app.seats.some((s) => s.lifeId === id && s.seatNo !== app.modalSeat)
}


function logo(p: { kind?: string; baseUrl?: string; label?: string } | null): string {
  const s = `${p?.baseUrl ?? ''} ${p?.label ?? ''}`.toLowerCase()
  if (s.includes('deepseek')) return '🐋'
  if (s.includes('moonshot') || s.includes('kimi')) return '🌙'
  if (s.includes('anthropic') || s.includes('claude')) return '🅰️'
  if (s.includes('openai')) return '⚪'
  if (s.includes('bigmodel') || s.includes('zhipu') || s.includes('glm')) return '🧠'
  if (s.includes('dashscope') || s.includes('qwen') || s.includes('通义')) return '☁️'
  if (s.includes('siliconflow')) return '💠'
  if (s.includes('openrouter')) return '🔀'
  return '🔌'
}

/** 按平台分组（外加一个"通用"，放没绑定平台的人——正常不会有）。 */
const cats = computed(() => {
  const out = app.providers.map((p) => ({
    key: p.id,
    label: p.label || p.name || '（未命名平台）',
    icon: logo(p),
    generic: false,
    list: app.lives.filter((l) => l.providerId === p.id && available(l.id)),
  }))
  const orphans = app.lives.filter((l) => available(l.id) && (!l.providerId || !app.providerById(l.providerId)))
  if (orphans.length) {
    out.push({ key: '', label: '平台未配置', icon: '🌐', generic: true, list: orphans })
  }
  return out.filter((c) => c.list.length)
})

const pickedCat = computed(() => cats.value.find((c) => c.key === cat.value) ?? null)
const pickedLife = computed(() => app.lives.find((l) => l.id === lifeId.value) ?? null)
const provider = computed(() =>
  pickedLife.value ? app.providerById(pickedLife.value.providerId) : null,
)
const models = computed(() => (provider.value?.models ?? []).map((m) => m.id))

function taglineOf(id: string): string {
  const life = app.lives.find((l) => l.id === id)
  if (!life) return ''
  const tpl = app.templateById(life.templateId)
  return tpl?.tagline || (life.templateBare ? '裸模型：不套人格，直接问模型本身' : life.templateName || '')
}

onMounted(() => {
  const cur = editing.value
  const life = cur ? app.lives.find((l) => l.id === cur.lifeId) : null
  if (life) {
    cat.value = app.providerById(life.providerId) ? life.providerId : ''
    lifeId.value = life.id
    model.value = cur?.model || life.model || ''
  } else {
    cat.value = cats.value[0]?.key ?? ''
  }
})

function pickCat(key: string) {
  cat.value = key

  if (!pickedCat.value?.list.some((l) => l.id === lifeId.value)) {
    lifeId.value = ''
    model.value = ''
  }
}

function pickLife(id: string) {
  lifeId.value = id
  const life = app.lives.find((l) => l.id === id)
  // 选人时先用实体默认模型；下方高级设置仍可覆盖本次会话的模型。
  model.value = life?.model || ''
}

async function reloadModels() {
  const p = pickedLife.value?.providerId
  if (!p) return
  fetching.value = true
  problem.value = ''
  try {
    const got = await api.fetchModels(p)
    const prov = app.providerById(p)
    if (prov) {
      prov.models = got.models
      prov.modelsFetchedAt = got.modelsFetchedAt
    }
    if (!model.value && got.models.length) model.value = got.models[0].id
  } catch (e) {
    problem.value = (e instanceof Error ? e.message : String(e)) + '（可以直接手填模型名）'
  } finally {
    fetching.value = false
  }
}

async function apply() {
  // 保留其他席位的模型覆盖值，统一由 store 保存并重新编号；不修改实体默认模型。
  if (!lifeId.value) {
    problem.value = '先选一位数字人'
    return
  }
  const chosen = { lifeId: lifeId.value, model: model.value }
  const next = app.seats.map((s) => s.seatNo === seatNo.value
    ? chosen : { lifeId: s.lifeId, model: s.model })
  if (isNew.value) next.push(chosen)
  try {
    await app.setSeats(next)
    app.closeModal()
    await app.loadRounds()
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeSeat() {
  await app.removeSeat(seatNo.value)
  app.closeModal()
}
</script>

<template>
  <div class="modal-mask" @click.self="app.closeModal()">
    <div class="modal wide">
      <div class="modal-head">
        <h3>{{ isNew ? '加一位' : '编辑席位' }}</h3>
        <div style="flex: 1"></div>
        <button class="btn tiny" @click="app.closeModal()">关闭</button>
      </div>

      <div class="modal-body">
        <div class="field">
          <label>选数字人</label>

          <div v-if="!cats.length" class="hint">
            没有可添加的数字人。可以先去「数字人库」创建一位。
          </div>

          <template v-else>
            <!-- ① 先选平台 -->
            <div class="pick-cats">
              <div
                v-for="c in cats"
                :key="c.key"
                class="pick-cat"
                :class="{ on: c.key === cat }"
                @click="pickCat(c.key)"
              >
                <span class="pc-emoji">{{ c.icon }}</span>
                <div class="pc-body">
                  <div class="pc-name">{{ c.label }}</div>
                  <div class="pc-sub">{{ c.list.length }} 位数字人</div>
                </div>
                <span v-if="c.key === cat" class="pc-tick">✓</span>
              </div>
            </div>

            <!-- ② 再看这一家的人 -->
            <div class="pick-grid">
              <div
                v-for="l in pickedCat?.list ?? []"
                :key="l.id"
                class="pick-cell"
                :class="{ on: l.id === lifeId }"
                @click="pickLife(l.id)"
              >
                <span class="pc-emoji">{{ l.emoji }}</span>
                <div class="pc-body">
                  <div class="pc-name">{{ l.name }}</div>
                  <div class="pc-sub">{{ taglineOf(l.id) || '（没有简介）' }}</div>
                </div>
              </div>
            </div>
          </template>
        </div>

        <div v-if="problem" class="answer-note">ℹ {{ problem }}</div>

        <!-- 收起来：一般不用动 -->
        <details v-if="pickedLife" class="advanced">
          <summary>高级设置</summary>
          <div class="advanced-body">
            <div class="field">
              <label>模型</label>
              <div class="row" style="gap: 10px; align-items: center">
                <select v-if="models.length" v-model="model" style="flex: 1">
                  <option value="">（用他的默认模型 {{ pickedLife.model || '—' }}）</option>
                  <option v-if="model && !models.includes(model)" :value="model">{{ model }}</option>
                  <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
                </select>
                <input
                  v-else
                  v-model="model"
                  type="text"
                  placeholder="这家还没有模型列表，手填一个模型名"
                  style="flex: 1"
                />
                <button class="btn tiny" :disabled="fetching" @click="reloadModels">
                  {{ fetching ? '拉取中…' : '重新拉取' }}
                </button>
              </div>
              <div class="hint">
                留空就用这位数字人自己的默认模型（{{ pickedLife.model || '—' }}）；
                这里选择的模型只用于本次会话。
              </div>
            </div>
          </div>
        </details>
      </div>

      <div class="modal-foot">
        <button v-if="!isNew" class="btn danger" @click="removeSeat">移除席位</button>
        <div style="flex: 1"></div>
        <button class="btn" @click="app.closeModal()">取消</button>
        <button class="btn primary" :disabled="!lifeId" @click="apply">
          {{ isNew ? '加进来' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>
