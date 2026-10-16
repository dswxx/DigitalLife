<script setup lang="ts">
/**
 * 数字人库。版面就两件事，**用横向空间**（不是一人一行）：
 *
 *   上半：人格模板（蓝图，不绑厂商、没有记忆）
 *   下半：按厂商分组的个体（真正活着的那些，各有各的记忆）
 *
 * 「通用（裸模型直连）」并进模板区——裸模型本来就是"没有系统提示词的模板"，
 * 用它创建的个体落在对应厂商那一栏里。所以分类只剩两种语义。
 */
import { computed, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'
import CreateWizard from '../components/CreateWizard.vue'

const app = useApp()
const emit = defineEmits<{ (e: 'open-chat'): void }>()

const wizard = ref(false)
const editing = ref<null | { id: string; name: string; emoji: string; tagline: string; system: string; bare: boolean }>(null)
const problem = ref('')
const confirmDelete = ref('')

const groups = computed(() => app.livesByProvider.filter((g) => g.lives.length || g.provider))

function when(ms: number): string {
  if (!ms) return '还没聊过'
  const diff = Date.now() - ms
  if (diff < 90_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 2 * 86_400_000) return '昨天'
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  const d = new Date(ms)
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`
}

async function chatWith(lifeId: string) {
  const session = await app.ensureSession()
  const already = session.seats.some((s) => s.lifeId === lifeId)
  if (!already) {
    await app.setSeats([...session.seats.map((s) => ({ lifeId: s.lifeId, model: s.model })), { lifeId }])
  } else {
    app.syncSeatsFromSession()
  }
  app.view = 'chat'
  emit('open-chat')
}

async function saveTemplate() {
  if (!editing.value) return
  try {
    await api.updateTemplate(editing.value.id, {
      name: editing.value.name,
      emoji: editing.value.emoji,
      tagline: editing.value.tagline,
      system: editing.value.system,
      bare: editing.value.bare,
    })
    await Promise.all([app.refreshTemplates(), app.refreshLives()])
    editing.value = null
    problem.value = ''
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeLife(id: string) {
  try {
    await api.deleteLife(id)
    await app.refreshLives()
    confirmDelete.value = ''
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeTemplate(id: string) {
  try {
    await api.deleteTemplate(id)
    await app.refreshTemplates()
    problem.value = ''
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}
</script>

<template>
  <div class="row" style="align-items: flex-start">
    <div>
      <h1>数字人库</h1>
      <p class="hint">{{ app.templates.length }} 份人格蓝图 · {{ app.lives.length }} 位活着的个体</p>
    </div>
    <div class="spacer"></div>
    <button class="btn primary" @click="wizard = true">＋ 新建</button>
  </div>

  <p v-if="problem" class="note bad" style="margin-top: 12px">{{ problem }}</p>

  <!-- ── 人格模板 ── -->
  <h2>人格模板<span class="hint" style="display: inline; margin-left: 8px">蓝图：不绑厂商、没有记忆</span></h2>
  <div class="tpl-grid">
    <button v-for="t in app.templates" :key="t.id" class="tpl" @click="editing = { ...t }">
      <div class="emoji">{{ t.emoji }}</div>
      <div class="name">
        {{ t.name }}
        <span v-if="t.bare" class="tag bare">裸模型</span>
      </div>
      <div class="tagline">{{ t.tagline || (t.bare ? '不发人格，看模型本来的样子' : '（没有一句话说明）') }}</div>
      <div class="count mono">{{ app.templateUsage(t.id) }} 个生命 · 点开可改</div>
    </button>
  </div>

  <!-- ── 各家厂商的个体 ── -->
  <template v-if="app.lives.length">
    <h2>活着的个体<span class="hint" style="display: inline; margin-left: 8px">同一份人格 + 某家 API = 一个有自己记忆的个体</span></h2>
    <div v-for="g in groups" :key="g.providerId" class="vendor">
      <div class="vendor-head">
        <span class="dot"></span>
        <strong>{{ g.provider?.label || g.provider?.name || '（平台已删除）' }}</strong>
        <span class="n">{{ g.lives.length }} 位</span>
      </div>
      <div v-if="!g.lives.length" class="empty">这家平台上还没有数字人</div>
      <div v-else class="life-grid">
        <div v-for="l in g.lives" :key="l.id" class="life">
          <div class="head">
            <span class="emoji">{{ l.emoji }}</span>
            <span class="name">{{ l.name }}</span>
            <span v-if="l.templateBare" class="tag bare">裸</span>
            <div class="spacer"></div>
            <span class="mono">{{ l.model }}</span>
          </div>
          <div class="from">← {{ l.templateName || '（模板已删除）' }}</div>
          <div class="stats">
            <span>聊过 {{ l.sessionCount }} 次</span>
            <span>记 {{ l.memoryCount }} 条</span>
            <span>{{ when(l.lastTalkedAt) }}</span>
          </div>
          <div class="acts">
            <button class="btn sm primary" @click="chatWith(l.id)">聊</button>
            <button class="btn sm" @click="app.openDrawer('memory', l.id)">记忆</button>
            <button class="btn sm" @click="app.openDrawer('timeline', l.id)">一生</button>
            <div class="spacer"></div>
            <button
              v-if="confirmDelete !== l.id"
              class="btn sm ghost"
              title="删掉他（连同他的记忆）"
              @click="confirmDelete = l.id"
            >
              ✕
            </button>
            <template v-else>
              <button class="btn sm danger" @click="removeLife(l.id)">真的删掉</button>
              <button class="btn sm ghost" @click="confirmDelete = ''">算了</button>
            </template>
          </div>
        </div>
      </div>
    </div>
  </template>

  <div v-else class="empty" style="margin-top: 20px">
    还没有活着的数字人。点右上角「＋ 新建」——从一份人格蓝图 + 一家 API 开始。
  </div>

  <!-- ── 模板编辑 ── -->
  <div v-if="editing" class="drawer-mask" @click.self="editing = null">
    <div class="drawer" style="width: min(680px, 94vw)">
      <div class="drawer-head">
        <strong>改这份人格蓝图</strong>
        <div class="spacer"></div>
        <span class="mono">{{ app.templateUsage(editing.id) }} 位在用</span>
        <button class="btn sm ghost" @click="editing = null">关闭</button>
      </div>
      <div class="drawer-body">
        <div class="grid2">
          <div class="field"><label>名字</label><input v-model="editing.name" type="text" /></div>
          <div class="field"><label>图标</label><input v-model="editing.emoji" type="text" /></div>
        </div>
        <div class="field"><label>一句话</label><input v-model="editing.tagline" type="text" /></div>
        <div class="field" style="align-items: start">
          <label>系统提示词</label>
          <textarea v-model="editing.system" rows="14" style="font-family: var(--mono); font-size: 12.5px"></textarea>
        </div>
        <label class="row" style="gap: 8px; margin: 8px 0 4px">
          <input type="checkbox" v-model="editing.bare" style="width: auto" />
          <span>纯基线：请求里一个 system 字段都不发（连时间感和记忆也不注入）</span>
        </label>
        <div class="note">
          改了内容会生成**新的**模板（id 由名字+正文算出来），用着它的 {{ app.templateUsage(editing.id) }} 位会**自动跟着走**；
          旧文件会被删掉，不会在库里留两份看着像多了一个。
        </div>
      </div>
      <div class="row" style="padding: 12px 18px; border-top: 1px solid var(--line); background: var(--panel)">
        <button class="btn danger" @click="removeTemplate(editing.id)">删掉这份蓝图</button>
        <div class="spacer"></div>
        <button class="btn primary" @click="saveTemplate">保存</button>
      </div>
    </div>
  </div>

  <CreateWizard v-if="wizard" @close="wizard = false" @created="chatWith" />
</template>
