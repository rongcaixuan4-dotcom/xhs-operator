<script setup>
// 采集与选题：话题卡片 + 监测日志时间线 + 已采集/已入选双列表（PRD 页面 4）
import { onMounted, ref } from 'vue'
import { apiGet, apiPost, fileUrl } from '../api.js'

const topics = ref([])
const collectedList = ref([])
const selectedList = ref([])
const collectLog = ref([])
const collecting = ref(false)

const topicForm = ref({ keyword: '', priority_weight: 1.0, daily_quota: 5, collect_times: '09:00,14:00,19:00', enabled: true })
const editingTopic = ref(null)

async function load() {
  topics.value = await apiGet('/api/topics')
  const [col, sel] = await Promise.all([
    apiGet('/api/collected?status=collected&page_size=20'),
    apiGet('/api/collected?status=selected&page_size=20'),
  ])
  collectedList.value = col.items
  selectedList.value = sel.items
}

async function saveTopic() {
  const payload = {
    keyword: topicForm.value.keyword,
    priority_weight: Number(topicForm.value.priority_weight) || 1,
    daily_quota: Number(topicForm.value.daily_quota) || 5,
    collect_times: topicForm.value.collect_times.split(/[,，\s]+/).filter(Boolean),
    enabled: topicForm.value.enabled,
  }
  if (editingTopic.value) await apiPut(`/api/topics/${editingTopic.value.id}`, payload)
  else await apiPost('/api/topics', payload)
  topicForm.value = { keyword: '', priority_weight: 1.0, daily_quota: 5, collect_times: '09:00,14:00,19:00', enabled: true }
  editingTopic.value = null
  await load()
}

function editTopic(t) {
  editingTopic.value = t
  topicForm.value = {
    keyword: t.keyword,
    priority_weight: t.priority_weight,
    daily_quota: t.daily_quota,
    collect_times: (t.collect_times || []).join(','),
    enabled: t.enabled,
  }
}

async function removeTopic(t) {
  if (!confirm(`删除话题「${t.keyword}」？`)) return
  const { apiDelete } = await import('../api.js')
  await apiDelete(`/api/topics/${t.id}`)
  await load()
}

async function toggleTopic(t) {
  await apiPut(`/api/topics/${t.id}`, { ...t, collect_times: t.collect_times, enabled: !t.enabled })
  await load()
}

async function collectNow() {
  collecting.value = true
  try {
    const data = await apiPost('/api/topics/collect-now', {})
    collectLog.value.unshift({
      time: new Date().toLocaleTimeString(),
      ok: true,
      text: `手动采集完成：新增 ${data.total_added} 篇，新入选 ${data.newly_selected} 篇`,
      detail: (data.results || []).map((r) => `${r.topic}: ${r.ok ? `+${r.added}` : `失败 ${r.error}`}`).join('；'),
    })
    await load()
  } catch (e) {
    collectLog.value.unshift({ time: new Date().toLocaleTimeString(), ok: false, text: '手动采集失败', detail: String(e.message || e) })
  } finally {
    collecting.value = false
  }
}

async function selectNote(n) {
  await apiPost(`/api/collected/${n.id}/select`)
  await load()
}
async function discardNote(n) {
  await apiPost(`/api/collected/${n.id}/discard`)
  await load()
}

function statusTag(s) {
  return { collected: 'warn', selected: 'ok', discarded: 'plain' }[s] || 'plain'
}

onMounted(load)
</script>

<template>
  <div>
    <!-- 话题卡片 -->
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <h3 style="margin:0">监测话题（{{ topics.length }}）</h3>
        <button class="btn primary" :disabled="collecting" @click="collectNow">
          {{ collecting ? '采集中…' : '⚡ 立即采集一轮' }}
        </button>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">
        <div v-for="t in topics" :key="t.id" style="border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="display:flex;justify-content:space-between">
            <b># {{ t.keyword }}</b>
            <span class="tag" :class="t.enabled ? 'ok' : 'plain'">{{ t.enabled ? '启用' : '停用' }}</span>
          </div>
          <div style="color:var(--text-sub);font-size:12px;margin:6px 0">
            时段：{{ (t.collect_times || []).join(' / ') }}<br />
            每日 {{ t.daily_quota }} 篇 · 权重 {{ t.priority_weight }}
          </div>
          <div style="display:flex;gap:6px">
            <button class="btn small" @click="toggleTopic(t)">{{ t.enabled ? '停用' : '启用' }}</button>
            <button class="btn small" @click="editTopic(t)">编辑</button>
            <button class="btn small danger" @click="removeTopic(t)">删除</button>
          </div>
        </div>
      </div>
      <!-- 话题表单 -->
      <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
        <b>{{ editingTopic ? `编辑话题：${editingTopic.keyword}` : '新增话题' }}</b>
        <div style="display:grid;grid-template-columns:1fr 100px 100px 1fr auto;gap:8px;margin-top:8px">
          <input type="text" v-model="topicForm.keyword" placeholder="关键词，如：出租屋改造" />
          <input type="number" v-model="topicForm.daily_quota" min="1" max="50" title="每日篇数" />
          <input type="number" v-model="topicForm.priority_weight" step="0.1" min="0.1" max="3" title="评分权重" />
          <input type="text" v-model="topicForm.collect_times" title="采集时段（逗号分隔 HH:MM）" />
          <button class="btn primary" @click="saveTopic">{{ editingTopic ? '保存' : '添加' }}</button>
        </div>
      </div>
    </div>

    <div class="grid-2" style="margin-top:16px">
      <!-- 今日监测日志 -->
      <div class="card">
        <h3>今日监测日志</h3>
        <div class="timeline" v-if="collectLog.length">
          <div v-for="(l, i) in collectLog" :key="i" class="timeline-item" :class="l.ok ? 'active' : 'error'">
            <b>{{ l.time }}</b> {{ l.text }}
            <div v-if="l.detail" style="color:var(--text-sub);font-size:12px">{{ l.detail }}</div>
          </div>
        </div>
        <div class="empty" v-else>点击「立即采集一轮」或等待定时触发（时段任务日志也会由后端写入 logs/app.log）</div>
      </div>

      <!-- 已采集 / 已入选 双列表 -->
      <div class="card">
        <h3>已采集（{{ collectedList.length }}） / 已入选（{{ selectedList.length }}）</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div>
            <b style="font-size:13px">已采集</b>
            <div v-for="n in collectedList.slice(0, 8)" :key="n.id"
                 style="display:flex;gap:8px;padding:8px 0;border-bottom:1px solid var(--border)">
              <img class="thumb" :src="n.local_images[0] ? fileUrl(n.local_images[0]) : ''" v-if="n.local_images.length" />
              <div style="flex:1;min-width:0">
                <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ n.title }}</div>
                <div style="font-size:12px;color:var(--text-sub)">
                  ❤ {{ n.likes }} · 匹配分 {{ n.match_score }}
                </div>
                <div style="margin-top:4px;display:flex;gap:6px">
                  <button class="btn small primary" @click="selectNote(n)">加选</button>
                  <button class="btn small danger" @click="discardNote(n)">排除</button>
                </div>
              </div>
            </div>
            <div v-if="!collectedList.length" class="empty">暂无</div>
          </div>
          <div>
            <b style="font-size:13px">已入选</b>
            <div v-for="n in selectedList.slice(0, 8)" :key="n.id"
                 style="display:flex;gap:8px;padding:8px 0;border-bottom:1px solid var(--border)">
              <img class="thumb" :src="n.local_images[0] ? fileUrl(n.local_images[0]) : ''" v-if="n.local_images.length" />
              <div style="flex:1;min-width:0">
                <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ n.title }}</div>
                <div style="font-size:12px;color:var(--text-sub)">❤ {{ n.likes }} · 匹配分 {{ n.match_score }}</div>
                <span class="tag ok" style="margin-top:4px">待二创</span>
              </div>
            </div>
            <div v-if="!selectedList.length" class="empty">暂无</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
