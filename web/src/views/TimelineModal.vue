<script setup lang="ts">
/**
 * 「一生」：这位数字人的时间线。
 *
 * **按天分组**，不是流水账列表——他要活很久，流水账翻不动。
 * 每天两样东西：那天和他说过话的会话（能点回圆桌）、那天记住的事。
 * 全部从记录和记忆算出来，不额外存一份（原文是真相，时间线是派生物）。
 */
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'

const app = useApp()
const days = ref<any[]>([])
const busy = ref(true)
const problem = ref('')
const life = ref<any>(null)

const typeName: Record<string, string> = {
  user: '关于他',
  self: '自己的判断',
  feedback: '他要求过我',
  reference: '外部事实',
}

onMounted(async () => {
  try {
    const [tl, detail] = await Promise.all([
      api.timeline(app.modalLifeId),
      api.life(app.modalLifeId),
    ])
    days.value = tl.days
    life.value = detail
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
})

async function compact(sid: string) {
  try {
    const got = await api.compact(app.modalLifeId, sid)
    problem.value = got.compaction
      ? `整理完成：${(got.lines ?? []).join('；')}`
      : '还没到该整理的水位——原文不算多，或者窗口还有余量。'
    const [tl, detail] = await Promise.all([
      api.timeline(app.modalLifeId),
      api.life(app.modalLifeId),
    ])
    days.value = tl.days
    life.value = detail
    await app.refreshLives()
  } catch (e) {
    problem.value = e instanceof Error ? e.message : String(e)
  }
}

function born(ms: number) {
  if (!ms) return '不知道什么时候出生'
  const d = new Date(ms)
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日出生`
}
</script>

<template>
  <div class="modal-mask" @click.self="app.closeModal()">
    <div class="modal wide">
      <div class="modal-head">
        <h3>{{ life?.identity?.emoji }} {{ life?.identity?.name }} 的一生</h3>
        <span class="pill">{{ born(life?.identity?.bornAt) }}</span>
        <div style="flex: 1"></div>
        <button class="btn tiny" @click="app.openModal('memory', app.modalLifeId)">看记忆</button>
        <button class="btn tiny" @click="app.closeModal()">关闭</button>
      </div>

      <div class="modal-body">
        <div class="row wrap" style="margin-bottom: 14px">
          <span class="pill">{{ life?.summary?.sessionCount ?? 0 }} 次对话</span>
          <span class="pill">{{ life?.summary?.memoryCount ?? 0 }} 条记忆</span>
          <span class="pill">← {{ life?.template?.name || '蓝图已删除' }}</span>
          <span class="pill">{{ life?.identity?.model }}</span>
        </div>

        <div v-if="problem" class="answer-note">ℹ {{ problem }}</div>
        <div v-if="busy" class="composer-hint">读取中…</div>

        <div v-else-if="!days.length" class="empty">
          <div class="big">🌱</div>
          <h3>还没有聊过</h3>
          <p>去圆桌跟他说说话。他每聊一次，这里就多一天。</p>
        </div>

        <div v-for="d in days" :key="d.day" class="tl-day">
          <div class="tl-date">{{ d.day }}</div>
          <div class="tl-items">
            <div v-for="s in d.sessions" :key="s.id" class="tl-ev">
              <div class="row">
                <b>聊了 {{ s.entries }} 段</b>
                <span v-if="s.compaction?.summary" class="pill ok">已压成摘要</span>
                <div style="flex: 1"></div>
                <button class="btn tiny" @click="compact(s.id)">现在整理一次</button>
              </div>
              <div v-if="s.compaction?.summary" class="tl-k">
                他自己记得的：{{ s.compaction.summary }}
              </div>
              <div v-if="s.compaction?.totalUsage" class="tl-k">
                整理花掉
                {{ (s.compaction.totalUsage.promptTokens || 0) + (s.compaction.totalUsage.completionTokens || 0) }}
                token（已记进账）
              </div>
            </div>
            <div v-for="m in d.memories" :key="m.id" class="tl-ev">
              <div class="row">
                <span class="pill">{{ typeName[m.type] ?? m.type }}</span>
                <b>{{ m.name }}</b>
              </div>
              <div class="tl-k">{{ m.desc }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
