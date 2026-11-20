<script setup>
// 复盘：周选择器 + Markdown 报告 + 改进项确认回流（PRD 页面 8）
import { computed, onMounted, ref } from 'vue'
import { marked } from 'marked'
import { apiGet, apiPost } from '../api.js'

const reports = ref([])
const currentId = ref(null)
const report = ref(null)
const confirming = ref(false)

const currentReport = computed(() => report.value)
const reportHtml = computed(() => {
  if (!report.value?.report_md) return ''
  return marked.parse(report.value.report_md)
})

const SCOPE_LABELS = { topic_weight: '话题权重', publish_time: '发布时间', prompt: '提示词' }

async function load() {
  const data = await apiGet('/api/reports')
  reports.value = data.items
  if (reports.value.length && currentId.value === null) {
    await open(reports.value[0].id)
  }
}

async function open(id) {
  currentId.value = id
  report.value = await apiGet(`/api/reports/${id}`)
}

const checked = ref(new Set())
function toggleInsight(idx) {
  if (checked.value.has(idx)) checked.value.delete(idx)
  else checked.value.add(idx)
  checked.value = new Set(checked.value)
}

async function confirmReport() {
  if (checked.value.size === 0) return alert('请至少勾选一条改进项')
  confirming.value = true
  try {
    const data = await apiPost(`/api/reports/${report.value.id}/confirm`, {
      insight_indexes: Array.from(checked.value),
    })
    alert(`已确认 ${data.confirmed} 条改进项，知识库新增 ${data.chunks_written} 个切片（下周一自动生效于二创提示词）`)
    checked.value = new Set()
    await open(report.value.id)
  } finally {
    confirming.value = false
  }
}

function statusTag(s) {
  return { pending_confirm: 'warn', confirmed: 'ok', expired: 'plain' }[s] || 'plain'
}

async function generateNow() {
  await apiPost('/api/reports/generate-now')
  await load()
}

onMounted(load)
</script>

<template>
  <div class="grid-2" style="grid-template-columns: 300px 1fr">
    <!-- 周选择器 -->
    <div class="card" style="align-self:start">
      <h3>历史周报</h3>
      <div v-for="r in reports" :key="r.id"
           style="padding:10px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;cursor:pointer"
           :style="currentId === r.id ? 'border-color:var(--primary);background:#fdf0f3' : ''"
           @click="open(r.id)">
        <div style="display:flex;justify-content:space-between">
          <b style="font-size:13px">{{ r.week_start }} ~ {{ r.week_end }}</b>
          <span class="tag" :class="statusTag(r.status)">
            {{ { pending_confirm: '待确认', confirmed: '已确认', expired: '已过期' }[r.status] }}
          </span>
        </div>
        <div style="font-size:12px;color:var(--text-sub);margin-top:4px">
          改进项 {{ (r.insights || []).length }} 条
        </div>
      </div>
      <div v-if="!reports.length" class="empty">
        暂无周报<br /><small>每周一 09:00 自动生成</small>
      </div>
      <button class="btn" style="width:100%" @click="generateNow">⚡ 立即生成上周报告</button>
    </div>

    <!-- 报告内容 -->
    <div v-if="currentReport">
      <div class="card">
        <h3>复盘报告（{{ currentReport.week_start }} ~ {{ currentReport.week_end }}）</h3>
        <div class="report-md" style="font-size:14px;line-height:1.8" v-html="reportHtml"></div>
      </div>

      <!-- 改进项确认 -->
      <div class="card">
        <h3>改进项（勾选确认后写入知识库生效）</h3>
        <div v-for="(it, idx) in currentReport.insights" :key="idx"
             style="display:flex;gap:10px;padding:10px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;align-items:flex-start"
             :style="it.confirmed ? 'opacity:0.6;background:#f6fffa' : ''">
          <input type="checkbox" :disabled="it.confirmed || currentReport.status !== 'pending_confirm'"
                 :checked="it.confirmed || checked.has(idx)" style="width:auto;margin-top:4px"
                 @change="toggleInsight(idx)" />
          <div style="flex:1">
            <span class="tag info">{{ SCOPE_LABELS[it.scope] || it.scope }}</span>
            <div style="margin-top:6px">{{ it.content }}</div>
            <div style="font-size:12px;color:var(--text-sub);margin-top:4px">预期影响：{{ it.impact }}</div>
          </div>
        </div>
        <div v-if="!currentReport.insights?.length" class="empty">报告未包含改进项</div>
        <button v-if="currentReport.status === 'pending_confirm'" class="btn primary" :disabled="confirming"
                @click="confirmReport">
          {{ confirming ? '写入中…' : '✓ 确认并写入知识库' }}
        </button>
        <div v-else-if="currentReport.status === 'confirmed'" class="tag ok" style="display:inline-block;padding:8px">
          ✓ 已确认回流（下次二创提示词自动注入）
        </div>
        <div v-else class="tag plain" style="display:inline-block;padding:8px">已过期（仅归档，不回流）</div>
      </div>
    </div>
    <div v-else class="card"><div class="empty">从左侧选择一份周报</div></div>
  </div>
</template>
