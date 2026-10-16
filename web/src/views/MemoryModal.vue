<script setup lang="ts">
/**
 * 记忆编辑器（老界面没有这一页，但样式跟老的那套走）。
 *
 * 设计文档里那句话是这一页的存在理由：
 * **长期记忆是「人要读要改的东西」——他记错了你能直接删掉那一条。**
 */
import { onMounted, ref } from 'vue'
import { api, type Memory } from '../api'
import { useApp } from '../store'

const app = useApp()
const items = ref<Memory[]>([])
const index = ref('')
const busy = ref(false)
const problem = ref('')
const editing = ref<Memory | null>(null)
const adding = ref(false)
const draft = ref({ name: '', desc: '', type: 'user', scope: 'persistent', text: '' })
const confirmDel = ref('')

const life = () => app.lifeById(app.modalLifeId)

const typeName: Record<string, string> = {
  user: '关于他',
  self: '我自己的判断',
  feedback: '他要求过我',
  reference: '外部事实',
}

async function load() {
  busy.value = true
  try {
    const got = await api.memories(app.modalLifeId)
    items.value = got.memories
    index.value = got.index
    problem.value = ''
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(load)

function day(ms: number) {
  if (!ms) return '—'
  const d = new Date(ms)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

async function saveEdit() {
  if (!editing.value) return
  try {
    await api.editMemory(app.modalLifeId, editing.value.id, {
      name: editing.value.name,
      desc: editing.value.desc,
      type: editing.value.type,
      scope: editing.value.scope,
      text: editing.value.text,
    })
    editing.value = null
    await Promise.all([load(), app.refreshLives()])
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

async function add() {
  if (!draft.value.name.trim() || !draft.value.text.trim()) {
    problem.value = '记忆要有标题和正文'
    return
  }
  try {
    await api.addMemory(app.modalLifeId, draft.value)
    draft.value = { name: '', desc: '', type: 'user', scope: 'persistent', text: '' }
    adding.value = false
    await Promise.all([load(), app.refreshLives()])
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

async function remove(mid: string) {
  try {
    await api.deleteMemory(app.modalLifeId, mid)
    confirmDel.value = ''
    await Promise.all([load(), app.refreshLives()])
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}
</script>

<template>
  <div class="modal-mask" @click.self="app.closeModal()">
    <div class="modal wide">
      <div class="modal-head">
        <h3>{{ life()?.emoji }} {{ life()?.name }} 的记忆</h3>
        <span class="pill">{{ items.length }} 条</span>
        <div style="flex: 1"></div>
        <button class="btn tiny" @click="adding = !adding">＋ 手动加一条</button>
        <button class="btn tiny" @click="app.openModal('timeline', app.modalLifeId)">看一生</button>
        <button class="btn tiny" @click="app.closeModal()">关闭</button>
      </div>

      <div class="modal-body">
        <p class="composer-hint" style="margin-bottom: 12px">
          这些是他长期记得的事。目录只放一行摘要（所以记忆几百条也不会挤爆对话窗口），正文在这里。
          <b>他记错了就删掉那一条</b>——这是他唯一的记忆来源。
        </p>
        <div v-if="problem" class="answer-note err">✕ {{ problem }}</div>

        <div v-if="adding" class="mem-item" style="margin-bottom: 14px">
          <div class="mem-head">
            <input v-model="draft.name" type="text" placeholder="短标题" style="flex: 1" />
            <select v-model="draft.type" style="width: 150px">
              <option v-for="(label, key) in typeName" :key="key" :value="key">{{ label }}</option>
            </select>
          </div>
          <div class="mem-desc">
            <input v-model="draft.desc" type="text" placeholder="一句话（会进目录，要能独立读懂）" />
          </div>
          <div class="mem-text">
            <textarea v-model="draft.text" rows="3" placeholder="正文"></textarea>
          </div>
          <div class="mem-foot">
            <div style="flex: 1"></div>
            <button class="btn tiny" @click="adding = false">算了</button>
            <button class="btn tiny primary" @click="add">写进去</button>
          </div>
        </div>

        <div v-if="busy" class="composer-hint">读取中…</div>
        <div v-else-if="!items.length" class="empty">
          <div class="big">📔</div>
          <h3>他还没有长期记忆</h3>
          <p>聊久一点，或者在「一生」那一页点「现在整理一次」；也可以按「＋ 手动加一条」自己写。</p>
        </div>

        <div class="mem-list">
          <div v-for="m in items" :key="m.id" class="mem-item">
            <div class="mem-head">
              <span class="mem-name">{{ m.name }}</span>
              <span class="pill">{{ typeName[m.type] ?? m.type }}</span>
              <span v-if="m.scope === 'session'" class="pill">只在那次话题里</span>
              <div style="flex: 1"></div>
              <span class="mono-sm">
                {{ day(m.at) }}{{ m.updated_at ? ` · ${day(m.updated_at)} 改过` : '' }}
              </span>
            </div>

            <template v-if="editing?.id !== m.id">
              <div class="mem-desc">{{ m.desc }}</div>
              <div class="mem-text">{{ m.text }}</div>
            </template>
            <template v-else>
              <div class="mem-desc"><input v-model="editing.desc" type="text" /></div>
              <div class="mem-text"><textarea v-model="editing.text" rows="4"></textarea></div>
            </template>

            <div class="mem-foot">
              <span>来自 {{ m.source || '手动' }}</span>
              <div style="flex: 1"></div>
              <template v-if="editing?.id === m.id">
                <button class="btn tiny" @click="editing = null">取消</button>
                <button class="btn tiny primary" @click="saveEdit">保存</button>
              </template>
              <template v-else>
                <button class="btn tiny" @click="editing = { ...m }">改</button>
                <button v-if="confirmDel !== m.id" class="btn tiny" @click="confirmDel = m.id">删</button>
                <button v-else class="btn tiny danger" @click="remove(m.id)">真的删掉</button>
              </template>
            </div>
          </div>
        </div>

        <details v-if="index" style="margin-top: 18px">
          <summary class="composer-hint" style="cursor: pointer">
            看看他对话时实际读到的目录（{{ index.split('\n').filter(Boolean).length }} 行）
          </summary>
          <pre class="mono-sm" style="white-space: pre-wrap; margin-top: 8px">{{ index }}</pre>
        </details>
      </div>
    </div>
  </div>
</template>
