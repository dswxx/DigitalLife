<script setup lang="ts">
/**
 * 这次问谁：把谁加进来、他用哪个模型。
 *
 * 老界面也是这么做的（点卡片弹出来选），所以照它来：
 * 按平台分组的可选列表 + 这家有哪些模型。
 * `modalSeat === 0` 表示"新加一个座位"。
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'

const app = useApp()
const providerId = ref('')
const lifeId = ref('')
const model = ref('')
const models = ref<{ id: string }[]>([])
const fetching = ref(false)
const problem = ref('')

const isNew = computed(() => !app.modalSeat)

onMounted(() => {
  const cur = app.seats.find((s) => s.seatNo === app.modalSeat)
  lifeId.value = cur?.lifeId ?? ''
  model.value = cur?.model ?? ''
  const life = app.lifeById(lifeId.value)
  providerId.value = life?.providerId ?? app.providers[0]?.id ?? ''
  void loadModels()
})

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

function pickLife(id: string) {
  lifeId.value = id
  const life = app.lifeById(id)
  if (life) {
    providerId.value = life.providerId
    if (!model.value) model.value = life.model
  }
}

async function apply() {
  if (!lifeId.value) {
    problem.value = '先选一位数字人'
    return
  }
  const next = app.seats.map((s) => ({ lifeId: s.lifeId, model: s.model }))
  if (isNew.value) next.push({ lifeId: lifeId.value, model: model.value })
  else {
    const i = next.findIndex((_, k) => app.seats[k].seatNo === app.modalSeat)
    if (i >= 0) next[i] = { lifeId: lifeId.value, model: model.value }
  }
  await app.setSeats(next)
  app.closeModal()
  await app.loadRounds()
}
</script>

<template>
  <div class="modal-mask" @click.self="app.closeModal()">
    <div class="modal">
      <div class="modal-head">
        <h3>{{ isNew ? '加一位' : `换第 ${app.modalSeat} 位` }}</h3>
        <div style="flex: 1"></div>
        <button class="btn tiny" @click="app.closeModal()">关闭</button>
      </div>

      <div class="modal-body">
        <div v-if="!app.lives.length" class="empty">
          <div class="big">🎭</div>
          <h3>库里还没有数字人</h3>
          <p>先去「数字人库」用一份蓝图 + 一家平台创建一位。</p>
        </div>

        <template v-else>
          <div v-for="g in app.livesByProvider" :key="g.providerId">
            <div v-if="g.lives.length" class="opt-group-head">
              🔌 {{ g.provider?.label || g.provider?.name || '（平台已删除）' }}
            </div>
            <div class="row wrap" style="gap: 6px; padding: 0 4px 8px">
              <button
                v-for="l in g.lives"
                :key="l.id"
                class="btn tiny"
                :class="{ primary: lifeId === l.id }"
                @click="pickLife(l.id)"
              >
                {{ l.emoji }} {{ l.name }}
                <span class="mono-sm" style="margin-left: 5px">{{ l.model }}</span>
              </button>
            </div>
          </div>

          <div class="field" style="margin-top: 12px">
            <label>模型</label>
            <div class="row">
              <select v-if="models.length" v-model="model" style="flex: 1">
                <option v-for="m in models" :key="m.id" :value="m.id">{{ m.id }}</option>
              </select>
              <input
                v-else
                v-model="model"
                type="text"
                placeholder="这家没给模型列表，手动填模型名"
                style="flex: 1"
              />
              <button class="btn tiny" :disabled="fetching" @click="loadModels">
                {{ fetching ? '拉取中…' : '重新拉取' }}
              </button>
            </div>
            <span class="hint">留空就用这位数字人自己的默认模型。</span>
          </div>
          <div v-if="problem" class="answer-note">ℹ {{ problem }}</div>
        </template>
      </div>

      <div class="modal-foot">
        <div style="flex: 1"></div>
        <button class="btn" @click="app.closeModal()">取消</button>
        <button class="btn primary" :disabled="!lifeId" @click="apply">
          {{ isNew ? '加进来' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>
