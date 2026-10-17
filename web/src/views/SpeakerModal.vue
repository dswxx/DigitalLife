<script setup lang="ts">
/**
 * 加一位 / 编辑席位。**骨架就是老界面那个弹窗**：
 *
 *   .field > label 选数字人
 *     .pick-cats   先点平台（一家一个方块，带「N 位数字人」）
 *     .pick-grid   再看这家的人（.pick-cell：图标 + 名字 + 一句话）
 *   .advanced      高级设置（模型 / 重新拉取）——收起来，别碍事
 *   .modal-foot    移除席位 · 取消 · 加进来/保存
 *
 * 为什么分两级而不是一路铺开：数字人一多就得一路往下滚，而且看不出谁归属哪一家。
 * 「同名不同人」也只有先看到分类才明白——同一个查理·芒格在两家平台上各是一个个体。
 */
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

/** 平台图标：认得出是哪家就够了（老界面也是这么干的小标记）。 */
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
    list: app.lives.filter((l) => l.providerId === p.id),
  }))
  const orphans = app.lives.filter((l) => !l.providerId || !app.providerById(l.providerId))
  if (orphans.length) {
    out.push({ key: '', label: '通用（裸模型直连）', icon: '🌐', generic: true, list: orphans })
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
    cat.value = life.providerId || ''
    lifeId.value = life.id
    model.value = cur?.model || life.model || ''
  } else {
    cat.value = cats.value[0]?.key ?? ''
  }
})

function pickCat(key: string) {
  cat.value = key
  // 换了平台，原来选的人就不在这一类里了
  if (!pickedCat.value?.list.some((l) => l.id === lifeId.value)) {
    lifeId.value = ''
    model.value = ''
  }
}

function pickLife(id: string) {
  lifeId.value = id
  const life = app.lives.find((l) => l.id === id)
  // 席位上的模型**默认跟着人走**；只有人自己没有默认模型时才需要在这儿挑
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
  if (!lifeId.value) {
    problem.value = '先选一位数字人'
    return
  }
  const next = app.seats
    .filter((s) => s.seatNo !== seatNo.value)
    .map((s) => ({ lifeId: s.lifeId, model: s.model }))
  next.push({ lifeId: lifeId.value, model: model.value })
  await app.setSeats(next)
  app.closeModal()
  await app.loadRounds()
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
            数字人库是空的。先去「数字人库」用一份蓝图 + 一家平台创建一位。
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
                平台和模型都跟着数字人走，换模型去「数字人库」改。
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
