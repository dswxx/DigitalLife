<script setup lang="ts">
/**
 * 记忆编辑器：看目录、读正文、改、删、手动加一条。
 *
 * 设计文档里那句话是这一页的存在理由：
 * **长期记忆是「人要读要改的东西」——他记错了你能直接删掉那一条。**
 * 所以每条记忆都能展开看到正文、看到它从哪次会话来的、什么时候写的。
 */
import { onMounted, ref } from 'vue'
import { api, type Memory } from '../api'
import { useApp } from '../store'

const props = defineProps<{ lifeId: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const app = useApp()

const items = ref<Memory[]>([])
const index = ref('')
const busy = ref(false)
const problem = ref('')
const editing = ref<Memory | null>(null)
const adding = ref(false)
const draft = ref({ name: '', desc: '', type: 'user', scope: 'persistent', text: '' })
const confirmDel = ref('')

const life = () => app.lifeById(props.lifeId)

async function load() {
  busy.value = true
  try {
    const got = await api.memories(props.lifeId)
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
    await api.editMemory(props.lifeId, editing.value.id, {
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
    await api.addMemory(props.lifeId, draft.value)
    draft.value = { name: '', desc: '', type: 'user', scope: 'persistent', text: '' }
    adding.value = false
    await Promise.all([load(), app.refreshLives()])
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

async function remove(mid: string) {
  try {
    await api.deleteMemory(props.lifeId, mid)
    confirmDel.value = ''
    await Promise.all([load(), app.refreshLives()])
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

const typeName: Record<string, string> = {
  user: '关于他',
  self: '我自己的判断',
  feedback: '他要求过我',
  reference: '外部事实',
}
</script>

<template>
  <div class="drawer-mask" @click.self="emit('close')">
    <div class="drawer">
      <div class="drawer-head">
        <span style="font-size: 18px">{{ life()?.emoji }}</span>
        <strong>{{ life()?.name }} 的记忆</strong>
        <span class="mono">{{ items.length }} 条</span>
        <div class="spacer"></div>
        <button class="btn sm" @click="adding = !adding">＋ 手动加一条</button>
        <button class="btn sm ghost" @click="emit('close')">关闭</button>
      </div>

      <div class="drawer-body">
        <p class="hint" style="margin-bottom: 12px">
          这些是他长期记得的事。目录只放一行摘要（所以记忆几百条也不会挤爆对话窗口），
          正文在这里。**他记错了就删掉那一条**——这是他唯一的记忆来源。
        </p>
        <p v-if="problem" class="note bad">{{ problem }}</p>

        <div v-if="adding" class="card pad" style="margin-bottom: 14px">
          <div class="grid2">
            <div class="field"><label>标题</label><input v-model="draft.name" type="text" placeholder="短标题" /></div>
            <div class="field">
              <label>类型</label>
              <select v-model="draft.type">
                <option v-for="(label, key) in typeName" :key="key" :value="key">{{ label }}</option>
              </select>
            </div>
          </div>
          <div class="field"><label>一句话</label><input v-model="draft.desc" type="text" placeholder="会进目录，要能独立读懂" /></div>
          <div class="field" style="align-items: start">
            <label>正文</label>
            <textarea v-model="draft.text" rows="4"></textarea>
          </div>
          <div class="row">
            <div class="spacer"></div>
            <button class="btn ghost sm" @click="adding = false">算了</button>
            <button class="btn primary sm" @click="add">写进去</button>
          </div>
        </div>

        <div v-if="busy" class="empty">读取中…</div>
        <div v-else-if="!items.length" class="empty">
          他还没有长期记忆。<br />
          聊久一点，或者按「手动加一条」自己写一条给他。
        </div>

        <div v-for="m in items" :key="m.id" class="mem">
          <div class="head">
            <span class="name">{{ m.name }}</span>
            <span class="tag">{{ typeName[m.type] ?? m.type }}</span>
            <span v-if="m.scope === 'session'" class="tag">只在那次话题里</span>
            <div class="spacer"></div>
            <span class="mono" :title="m.source ? `来自会话 ${m.source}` : '手动写的'">
              {{ day(m.at) }}{{ m.updated_at ? ` · ${day(m.updated_at)} 改过` : '' }}
            </span>
          </div>
          <div v-if="editing?.id !== m.id" class="desc" style="padding: 8px 12px 0">{{ m.desc }}</div>
          <div v-if="editing?.id !== m.id" class="text">{{ m.text }}</div>

          <div v-if="editing?.id === m.id" style="padding: 10px 12px">
            <div class="field"><label>标题</label><input v-model="editing.name" type="text" /></div>
            <div class="field"><label>一句话</label><input v-model="editing.desc" type="text" /></div>
            <div class="field" style="align-items: start">
              <label>正文</label>
              <textarea v-model="editing.text" rows="4"></textarea>
            </div>
          </div>

          <div class="foot">
            <span class="mono">来自 {{ m.source || '手动' }}</span>
            <div class="spacer"></div>
            <template v-if="editing?.id === m.id">
              <button class="btn sm ghost" @click="editing = null">取消</button>
              <button class="btn sm primary" @click="saveEdit">保存</button>
            </template>
            <template v-else>
              <button class="btn sm" @click="editing = { ...m }">改</button>
              <button v-if="confirmDel !== m.id" class="btn sm ghost" @click="confirmDel = m.id">删</button>
              <template v-else>
                <button class="btn sm danger" @click="remove(m.id)">真的删掉</button>
                <button class="btn sm ghost" @click="confirmDel = ''">算了</button>
              </template>
            </template>
          </div>
        </div>

        <details v-if="index" style="margin-top: 18px">
          <summary class="hint" style="cursor: pointer">看看他对话时实际读到的目录（{{ index.split('\n').filter(Boolean).length }} 行）</summary>
          <pre class="mono" style="white-space: pre-wrap; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; margin-top: 8px">{{ index }}</pre>
        </details>
      </div>
    </div>
  </div>
</template>
