<script setup lang="ts">
/**
 * 「一生」：这位数字人的时间线。
 *
 * **按天分组**，不是流水账列表——他要活很久，流水账翻不动。
 * 每天两样东西：那天和他说过话的会话（能跳回去）、那天记住的事。
 * 全部从记录和记忆算出来，不额外存一份（原文是真相，时间线是派生物）。
 */
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { useApp } from '../store'

const props = defineProps<{ lifeId: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const app = useApp()

const days = ref<any[]>([])
const busy = ref(true)
const problem = ref('')
const life = ref<any>(null)

const typeName: Record<string, string> = {
  user: '关于他', self: '自己的判断', feedback: '他要求过我', reference: '外部事实',
}

onMounted(async () => {
  try {
    const [tl, detail] = await Promise.all([api.timeline(props.lifeId), api.life(props.lifeId)])
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
    const got = await api.compact(props.lifeId, sid)
    if (got.compaction) {
      problem.value = `整理完成：${got.lines?.join('；') ?? ''}`
    } else {
      problem.value = '还没到该整理的水位——原文不算多，或者窗口还有余量。'
    }
    const [tl, detail] = await Promise.all([api.timeline(props.lifeId), api.life(props.lifeId)])
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
  <div class="drawer-mask" @click.self="emit('close')">
    <div class="drawer">
      <div class="drawer-head">
        <span style="font-size: 18px">{{ life?.identity?.emoji }}</span>
        <strong>{{ life?.identity?.name }} 的一生</strong>
        <span class="mono">{{ born(life?.identity?.bornAt) }}</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" @click="emit('close')">关闭</button>
      </div>

      <div class="drawer-body">
        <div class="row wrap" style="margin-bottom: 14px">
          <span class="tag">{{ life?.summary?.sessionCount ?? 0 }} 次对话</span>
          <span class="tag">{{ life?.summary?.memoryCount ?? 0 }} 条记忆</span>
          <span class="tag">← {{ life?.template?.name || '模板已删除' }}</span>
          <span class="tag">{{ life?.identity?.model }}</span>
        </div>

        <p v-if="problem" class="note">{{ problem }}</p>
        <div v-if="busy" class="empty">读取中…</div>
        <div v-else-if="!days.length" class="empty">还没有聊过。去圆桌跟他说说话。</div>

        <div v-for="d in days" :key="d.day" class="day">
          <div class="date">{{ d.day }}</div>
          <div class="items">
            <div v-for="s in d.sessions" :key="s.id" class="ev">
              <div class="row">
                <strong>聊了 {{ s.entries }} 段</strong>
                <span v-if="s.compaction?.summary" class="tag">已压成摘要</span>
                <div class="spacer"></div>
                <button class="btn sm ghost" @click="compact(s.id)">现在整理一次</button>
              </div>
              <div v-if="s.compaction?.summary" class="k" style="margin-top: 6px">
                他自己记得的：{{ s.compaction.summary }}
              </div>
              <div v-if="s.compaction?.totalUsage" class="k" style="margin-top: 4px">
                整理花掉 {{ (s.compaction.totalUsage.promptTokens || 0) + (s.compaction.totalUsage.completionTokens || 0) }} token（已记进账）
              </div>
            </div>
            <div v-for="m in d.memories" :key="m.id" class="ev">
              <div class="row">
                <span class="tag">{{ typeName[m.type] ?? m.type }}</span>
                <strong>{{ m.name }}</strong>
              </div>
              <div class="k">{{ m.desc }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
