<script setup>
// 工作台：4 指标卡 + 流水线时间轴 + 今日发布/告警两栏（PRD 页面 1）
import { onMounted, ref } from 'vue'
import { apiGet, apiPost } from '../api.js'
import { store } from '../store.js'

const metrics = ref({ today_collected: 0, today_created: 0, today_published: 0, pending_review: 0 })
const pipeline = ref({ stage: 'idle', status: 'idle', detail: {}, stage_order: [] })
const todayPublish = ref([])
const alerts = ref([])
const loading = ref(false)

const stageLabels = {
  monitoring: '监测', collecting: '采集', selecting: '筛选', creating: '二创',
  reviewing: '审核', publishing: '发布', snapshot: '快照', done: '完成', idle: '空闲',
}

async function load() {
  const data = await apiGet('/api/dashboard/summary')
  metrics.value = data.metrics || metrics.value
  pipeline.value = data.pipeline || pipeline.value
  todayPublish.value = data.today_publish || []
  alerts.value = data.alerts || []
  store.todayPublishCount = metrics.value.today_published
  if (data.persona) {
    store.personaName = data.persona.name
    store.personaId = data.persona.id
  }
  if (data.ai_status) store.aiStatus = data.ai_status
}

async function triggerPipeline() {
  loading.value = true
  try {
    await apiPost('/api/dashboard/pipeline/run')
    setTimeout(load, 2000)
  } finally {
    loading.value = false
  }
}

function stageState(stage) {
  const info = pipeline.value.detail?.[stage]
  if (info && info.status === 'error') return 'error'
  if (pipeline.value.stage === stage && pipeline.value.status === 'running') return 'active'
  const idx = pipeline.value.stage_order.indexOf(stage)
  const curIdx = pipeline.value.stage_order.indexOf(pipeline.value.stage)
  if (idx >= 0 && curIdx > idx) return 'done'
  return ''
}

onMounted(() => { load(); setInterval(load, 15000) })
</script>

<template>
  <div>
    <!-- 4 指标卡 -->
    <div class="grid-4">
      <div class="metric"><div class="num">{{ metrics.today_collected }}</div><div class="label">今日采集</div></div>
      <div class="metric"><div class="num">{{ metrics.today_created }}</div><div class="label">今日二创</div></div>
      <div class="metric"><div class="num">{{ metrics.today_published }}</div><div class="label">今日发布</div></div>
      <div class="metric"><div class="num">{{ metrics.pending_review }}</div><div class="label">待审核</div></div>
    </div>

    <!-- 流水线状态时间轴 -->
    <div class="card" style="margin-top:16px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h3>流水线状态（{{ pipeline.status }}）</h3>
        <div style="display:flex;gap:8px">
          <button class="btn small" :disabled="loading" @click="triggerPipeline">▶ 立即运行</button>
          <button class="btn small" :disabled="loading" @click="triggerPipeline">⟳ 失败重试</button>
        </div>
      </div>
      <div class="timeline" style="margin-top:12px">
        <div v-for="s in pipeline.stage_order" :key="s" class="timeline-item" :class="stageState(s)">
          <b>{{ stageLabels[s] || s }}</b>
          <span v-if="pipeline.detail?.[s]" style="color:var(--text-sub);font-size:12px;margin-left:8px">
            <span v-if="pipeline.detail[s].status === 'error'" style="color:var(--err)">
              失败：{{ (pipeline.detail[s].error || (pipeline.detail[s].errors || []).join('；') || '未知') }}
            </span>
            <span v-else>完成{{ pipeline.detail[s].elapsed_ms ? `（${pipeline.detail[s].elapsed_ms}ms）` : '' }}</span>
          </span>
        </div>
      </div>
    </div>

    <!-- 今日发布 / 告警 两栏 -->
    <div class="grid-2">
      <div class="card">
        <h3>今日发布列表</h3>
        <table class="table" v-if="todayPublish.length">
          <thead><tr><th>时间</th><th>标题</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="t in todayPublish" :key="t.id">
              <td>{{ t.scheduled_at ? t.scheduled_at.slice(11, 16) : '-' }}</td>
              <td>{{ t.title }}<a v-if="t.note_url" :href="t.note_url" target="_blank" style="margin-left:6px">查看</a></td>
              <td>
                <span class="tag" :class="{ done: 'ok', queued: 'info', publishing: 'warn', verifying: 'warn', failed: 'err' }[t.status] || 'plain'">
                  {{ t.status }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">今日暂无发布任务</div>
      </div>
      <div class="card">
        <h3>告警通知（{{ alerts.length }}）</h3>
        <div v-if="alerts.length">
          <div v-for="(a, i) in alerts" :key="i" style="padding:8px 0;border-bottom:1px solid var(--border)">
            <span class="tag" :class="a.level === 'error' ? 'err' : 'warn'">{{ a.level === 'error' ? '错误' : '提醒' }}</span>
            <span style="margin-left:8px">{{ a.message }}</span>
            <div style="color:var(--text-sub);font-size:12px;margin-top:4px">{{ a.time }}</div>
          </div>
        </div>
        <div v-else class="empty">暂无告警 ✓</div>
      </div>
    </div>
  </div>
</template>
