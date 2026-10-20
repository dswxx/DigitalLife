<script setup lang="ts">
/** 人格蓝图、普通 AI 与数字人的管理入口，个体按平台分组。 */
import { computed, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'
import CreateWizard from '../components/CreateWizard.vue'

const app = useApp()

const wizard = ref(false)
const initialTemplateId = ref('')
const editing = ref<null | { id: string; name: string; emoji: string; tagline: string; system: string; bare: boolean }>(null)
const problem = ref('')
const confirmDel = ref('')

const groups = computed(() => app.livesByProvider)
const totalLives = computed(() => app.lives.length)

function createFrom(templateId = '') {
  initialTemplateId.value = templateId
  wizard.value = true
}

function addModel() {
  const bare = app.templates.find((template) => template.bare)
  createFrom(bare?.id ?? '')
}

function when(ms: number): string {
  if (!ms) return '还没聊过'
  const diff = Date.now() - ms
  if (diff < 90_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 2 * 86_400_000) return '昨天'
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  const d = new Date(ms)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

/** 把某位放到桌上并切过去——库那张卡上「聊」做的事。 */
async function chatWith(lifeId: string) {
  const session = await app.ensureSession()
  const has = session.seats.some((s) => s.lifeId === lifeId)
  if (!has) {
    await app.setSeats([
      ...session.seats.map((s) => ({ lifeId: s.lifeId, model: s.model })),
      { lifeId },
    ])
  } else {
    app.syncSeatsFromSession()
  }
  app.go('round')
  await app.loadRounds()
}

async function saveTemplate() {
  // 后端更新人格可能改变其内容 ID，并同步实体引用，所以两份概况都需要刷新。
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
    confirmDel.value = ''
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeTemplate(id: string) {
  try {
    await api.deleteTemplate(id)
    await app.refreshTemplates()
    problem.value = ''
    editing.value = null
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}
</script>

<template>
  <div class="stage-scroll">
    <div class="page">
      <div class="page-head">
        <h1>数字人库</h1>
        <p>
          {{ app.templates.length }} 份人格蓝图，{{ totalLives }} 位活着的数字人，分属
          {{ groups.length }} 个平台。可以直接比较不同 AI 的回答，也可以与带有人格和对话历史的数字人交流。
        </p>
      </div>

      <div class="row wrap" style="margin-bottom: 18px">
        <button class="btn primary" @click="createFrom()">＋ 新建数字人</button>
        <button class="btn" @click="addModel">＋ 添加 AI 模型</button>
        <button class="btn" @click="app.go('round')">去一问多答</button>
      </div>

      <div v-if="problem" class="answer-note err">✕ {{ problem }}</div>

      <!-- ── 方块一：人格蓝图 ── -->
      <div class="category-block">
        <div class="category-head">
          <span class="category-icon">📐</span>
          <span class="category-name">人格蓝图</span>
          <span class="pill">不绑厂商 · 没有记忆</span>
          <span class="pill">{{ app.templates.length }} 份</span>
          <div style="flex: 1"></div>
          <button class="btn tiny" @click="createFrom()">＋ 新建</button>
        </div>
        <div class="grid-cards">
          <div
            v-for="t in app.templates"
            :key="t.id"
            class="human-card is-tpl"
            style="cursor: pointer"
            @click="editing = { ...t }"
          >
            <div class="pc-head">
              <span class="pc-emoji">{{ t.emoji }}</span>
              <div>
                <div class="pc-name">{{ t.name }}</div>
                <div class="pc-sub">
                  {{ app.templateUsage(t.id) }} 位用着它<template v-if="t.bare"> · 裸模型</template>
                </div>
              </div>
            </div>
            <div class="pc-tag">
              {{ t.tagline || (t.bare ? '不发人格，看模型本来的样子' : '（没有一句话说明）') }}
            </div>
            <div class="pc-tags">
              <span v-for="tag in t.tags" :key="tag" class="pill">{{ tag }}</span>
              <span v-if="t.builtin" class="pill">内置</span>
            </div>
            <div class="pc-actions">
              <button class="btn tiny" @click.stop="editing = { ...t }">编辑</button>
              <button class="btn tiny" @click.stop="createFrom(t.id)">用它新建</button>
            </div>
          </div>
        </div>
      </div>

      <!-- ── 后面每个方块：一家平台，方块里是他的个体 ── -->
      <div v-for="g in groups" :key="g.providerId" class="category-block">
        <div class="category-head">
          <span class="category-icon">{{ g.lives[0]?.emoji ?? '🔌' }}</span>
          <span class="category-name">
            {{ g.provider?.label || g.provider?.name || '（平台已删除）' }}
          </span>
          <span class="pill">{{ g.lives.length }} 位</span>
          <div style="flex: 1"></div>
          <button class="btn tiny" @click="createFrom()">＋ 新建</button>
        </div>
        <div v-if="!g.lives.length" class="hint" style="padding: 15px">
          这家平台上还没有数字人。点右边「＋ 新建」从他的一份蓝图开始。
        </div>
        <div v-else class="grid-cards">
          <div v-for="l in g.lives" :key="l.id" class="human-card">
            <div class="pc-head">
              <span class="pc-emoji">{{ l.emoji }}</span>
              <div style="min-width: 0">
                <div class="pc-name">{{ l.name }}</div>
                <div class="pc-sub">← {{ l.templateName || '（蓝图已删除）' }} · {{ l.model }}</div>
              </div>
            </div>
            <div class="pc-tag">
              {{ when(l.lastTalkedAt) }}
            </div>
            <div class="pc-tags">
              <span class="pill">{{ l.templateBare ? '普通 AI · 本次对话' : '历史独立' }}</span>
            </div>
            <div class="pc-actions">
              <button class="btn tiny" @click="chatWith(l.id)">聊</button>
              <button
                v-if="confirmDel !== l.id"
                class="btn tiny"
                title="将这位数字人移入回收目录"
                @click="confirmDel = l.id"
              >
                ✕
              </button>
              <button v-else class="btn tiny danger" @click="removeLife(l.id)">
                真的删掉
              </button>
            </div>
          </div>
        </div>
      </div>

      <div v-if="!totalLives" class="hint" style="padding: 20px 0">
        还没有活着的数字人。点上面的「＋ 新建数字人」——先挑一份蓝图，再选一家平台。
      </div>
    </div>
  </div>


  <div v-if="editing" class="modal-mask" @click.self="editing = null">
    <div class="modal">
      <div class="modal-head">
        <h3>改这份人格蓝图</h3>
        <div style="flex: 1"></div>
        <span class="pill">{{ app.templateUsage(editing.id) }} 位在用</span>
      </div>
      <div class="modal-body">
        <div class="row" style="gap: 10px">
          <div class="field" style="flex: 1">
            <label>名字</label>
            <input v-model="editing.name" type="text" />
          </div>
          <div class="field" style="width: 90px">
            <label>图标</label>
            <input v-model="editing.emoji" type="text" />
          </div>
        </div>
        <div class="field">
          <label>一句话</label>
          <input v-model="editing.tagline" type="text" placeholder="会显示在库里的那张卡上" />
        </div>
        <div class="field">
          <label>系统提示词</label>
          <textarea v-model="editing.system" rows="12" style="font-family: var(--mono); font-size: 12.5px"></textarea>
        </div>
        <label class="check-row">
          <input type="checkbox" v-model="editing.bare" />
          <span>普通 AI：保留本次对话，不加载人格或其他会话的记录</span>
        </label>
        <p class="field hint">
          保存后，使用这份人格的 {{ app.templateUsage(editing.id) }} 位数字人都会采用新内容。
        </p>
      </div>
      <div class="modal-foot">
        <button class="btn danger" @click="removeTemplate(editing.id)">删掉这份蓝图</button>
        <div style="flex: 1"></div>
        <button class="btn" @click="editing = null">取消</button>
        <button class="btn primary" @click="saveTemplate">保存</button>
      </div>
    </div>
  </div>

  <CreateWizard v-if="wizard" :template-id="initialTemplateId" @close="wizard = false" @created="chatWith" />
</template>
