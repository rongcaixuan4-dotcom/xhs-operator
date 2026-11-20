<script setup>
// 数据看板：账号指标卡 + ECharts 趋势折线(CDN) + Top10 表 + 成功率（PRD 页面 7）
import { onMounted, ref } from 'vue'
import { apiGet, apiPost } from '../api.js'

const account = ref({ followers: 0, week_exposure: 0, week_interaction: 0, published_notes: 0 })
const trend = ref([])
const topNotes = ref([])
const success = ref({ by_status: {}, success_rate: null, fail_reasons: {} })
const snapshotting = ref(false)

async function load() {
  account.value = await apiGet('/api/stats/account')
  trend.value = await apiGet('/api/stats/trend?days=14')
  topNotes.value = await apiGet('/api/stats/top-notes')
  success.value = await apiGet('/api/stats/publish-success')
  renderChart()
}

function renderChart() {
  if (typeof window.echarts === 'undefined') return
  const el = document.getElementById('trend-chart')
  if (!el) return
  const chart = window.echarts.init(el)
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['互动总量', '点赞', '收藏', '评论'] },
    xAxis: { type: 'category', data: trend.value.map((d) => d.date.slice(5)) },
    yAxis: { type: 'value' },
    series: [
      { name: '互动总量', type: 'line', smooth: true, data: trend.value.map((d) => d.interaction), lineStyle: { color: '#e33d5c' }, itemStyle: { color: '#e33d5c' } },
      { name: '点赞', type: 'line', smooth: true, data: trend.value.map((d) => d.likes) },
      { name: '收藏', type: 'line', smooth: true, data: trend.value.map((d) => d.collects) },
      { name: '评论', type: 'line', smooth: true, data: trend.value.map((d) => d.comments) },
    ],
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
  })
}

async function snapshotNow() {
  snapshotting.value = true
  try {
    await apiPost('/api/stats/snapshot-now')
    await load()
  } finally {
    snapshotting.value = false
  }
}

const STATUS_LABELS = { queued: '排队', publishing: '发布中', verifying: '核查中', done: '成功', failed: '失败', canceled: '取消' }

onMounted(() => { load(); setTimeout(renderChart, 500) })
</script>

<template>
  <div>
    <!-- 账号指标卡 -->
    <div class="grid-4">
      <div class="metric"><div class="num">{{ account.followers }}</div><div class="label">粉丝数</div></div>
      <div class="metric"><div class="num">{{ account.week_exposure }}</div><div class="label">周曝光</div></div>
      <div class="metric"><div class="num">{{ account.week_interaction }}</div><div class="label">周互动</div></div>
      <div class="metric"><div class="num">{{ account.published_notes }}</div><div class="label">发帖数</div></div>
    </div>

    <!-- 趋势折线 -->
    <div class="card" style="margin-top:16px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h3>互动趋势（近 14 天）</h3>
        <button class="btn small" :disabled="snapshotting" @click="snapshotNow">
          {{ snapshotting ? '快照中…' : '⟳ 立即快照' }}
        </button>
      </div>
      <div id="trend-chart" style="width:100%;height:280px"></div>
      <div v-if="!trend.length" class="empty">暂无快照数据（发布笔记后每小时自动快照）</div>
    </div>

    <!-- Top10 -->
    <div class="card">
      <h3>笔记表现 Top10</h3>
      <table class="table" v-if="topNotes.length">
        <thead><tr><th>#</th><th>标题</th><th>曝光</th><th>赞</th><th>藏</th><th>评</th><th>快照时间</th></tr></thead>
        <tbody>
          <tr v-for="(n, i) in topNotes" :key="n.xhs_note_id || n.title">
            <td>{{ i + 1 }}</td>
            <td>{{ n.title }}</td>
            <td>{{ n.exposure }}</td>
            <td>{{ n.likes }}</td>
            <td>{{ n.collects }}</td>
            <td>{{ n.comments }}</td>
            <td>{{ n.captured_at.slice(0, 16).replace('T', ' ') }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无笔记数据</div>
    </div>

    <!-- 成功率 -->
    <div class="card">
      <h3>发布成功率与失败原因</h3>
      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <div>
          <span style="font-size:26px;font-weight:700;color:var(--ok)">
            {{ success.success_rate !== null ? (success.success_rate * 100).toFixed(0) + '%' : '—' }}
          </span>
          <span style="color:var(--text-sub);margin-left:6px">成功率</span>
        </div>
        <span v-for="(count, s) in success.by_status" :key="s" class="tag" :class="{ done: 'ok', failed: 'err', queued: 'info' }[s] || 'plain'">
          {{ STATUS_LABELS[s] || s }}：{{ count }}
        </span>
      </div>
      <div style="margin-top:10px" v-if="Object.keys(success.fail_reasons).length">
        <div v-for="(count, reason) in success.fail_reasons" :key="reason" style="font-size:13px;padding:4px 0">
          <span class="tag err">×{{ count }}</span> {{ reason }}
        </div>
      </div>
    </div>
  </div>
</template>
