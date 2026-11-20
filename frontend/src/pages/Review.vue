<script setup>
// 二创审核：左右对照 + 内联编辑 + 通过/驳回（PRD 页面 5）
import { onMounted, ref } from 'vue'
import { apiGet, apiPatch, apiPost, fileUrl } from '../api.js'
import { store } from '../store.js'

const filter = ref('pending_review')
const list = ref([])
const current = ref(null)   // {draft, original}
const saving = ref(false)

const STATUS_TABS = [
  { key: 'pending_review', label: '待审核' },
  { key: 'approved', label: '已通过' },
  { key: 'queued', label: '队列中' },
  { key: 'rejected', label: '已驳回' },
]

async function load() {
  const data = await apiGet(`/api/creations?status=${filter.value}&page_size=50`)
  list.value = data.items
  if (current.value) {
    const still = list.value.find((d) => d.id === current.value.draft.id)
    if (!still) current.value = null
  }
}

async function open(d) {
  current.value = await apiGet(`/api/creations/${d.id}`)
}

async function saveDraft() {
  if (!current.value) return
  saving.value = true
  try {
    const d = current.value.draft
    await apiPatch(`/api/creations/${d.id}`, { title: d.title, content: d.content })
    await load()
  } finally {
    saving.value = false
  }
}

async function approve() {
  const d = current.value.draft
  const data = await apiPost(`/api/creations/${d.id}/approve`)
  alert(`已通过，发布任务 #${data.task_id} 已创建（排期 ${data.scheduled_at || '自动'}）`)
  current.value = null
  await load()
}

async function reject() {
  const reason = prompt('请填写驳回原因（将写入知识库负样本）：')
  if (!reason) return
  await apiPost(`/api/creations/${current.value.draft.id}/reject`, { reason })
  current.value = null
  await load()
}

function statusTag(s) {
  return { generating: 'warn', pending_review: 'warn', approved: 'ok', queued: 'info', rejected: 'err' }[s] || 'plain'
}

onMounted(load)
</script>

<template>
  <div>
    <!-- 顶部筛选 -->
    <div class="card" style="display:flex;gap:8px;align-items:center;padding:12px">
      <button v-for="t in STATUS_TABS" :key="t.key" class="btn small"
              :class="filter === t.key ? 'primary' : ''" @click="filter = t.key; load()">{{ t.label }}</button>
      <span style="color:var(--text-sub);margin-left:8px">共 {{ list.length }} 条</span>
    </div>

    <!-- 对照审核区 -->
    <div class="grid-2" v-if="current" style="margin-top:16px">
      <!-- 左：原始笔记 -->
      <div class="card">
        <h3>原始笔记</h3>
        <img v-if="current.original && current.original.image_urls.length"
             :src="current.original.image_urls[0]" style="max-width:100%;max-height:320px;border-radius:8px" />
        <div v-else-if="current.original && current.original.local_images.length">
          <img :src="fileUrl(current.original.local_images[0])" style="max-width:100%;max-height:320px;border-radius:8px" />
        </div>
        <h4 style="margin:10px 0 6px">{{ current.original?.title }}</h4>
        <div style="color:var(--text-sub);font-size:13px">作者：{{ current.original?.author }} · ❤ {{ current.original?.likes }} · 藏 {{ current.original?.collects }} · 评 {{ current.original?.comments }}</div>
        <p style="margin-top:10px;white-space:pre-wrap;font-size:13px">{{ current.original?.content || '（列表采集无正文）' }}</p>
      </div>

      <!-- 右：二创稿（可编辑） -->
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0">二创稿 <span class="tag" :class="statusTag(current.draft.status)">{{ current.draft.status }}</span></h3>
          <span style="color:var(--text-sub);font-size:12px">相似度 {{ (current.draft.similarity_score * 100).toFixed(0) }}%（须 &lt;70%）</span>
        </div>
        <div v-if="current.draft.image_paths.length" style="display:flex;gap:8px;margin:10px 0;flex-wrap:wrap">
          <img v-for="p in current.draft.image_paths" :key="p" :src="fileUrl(p)"
               style="max-height:200px;max-width:180px;border-radius:8px;border:1px solid var(--border)" />
        </div>
        <div class="field" style="margin-top:10px">
          <label>标题（可内联编辑）</label>
          <input type="text" v-model="current.draft.title" />
        </div>
        <div class="field">
          <label>文案（可内联编辑）</label>
          <textarea v-model="current.draft.content" rows="8"></textarea>
        </div>
        <div v-if="current.draft.reject_reason" class="tag err" style="display:block;padding:8px">
          驳回原因：{{ current.draft.reject_reason }}
        </div>
        <div style="display:flex;gap:10px;margin-top:12px">
          <button class="btn" :disabled="saving" @click="saveDraft">💾 保存修改</button>
          <button class="btn primary" @click="approve">✓ 通过（入发布队列）</button>
          <button class="btn danger" @click="reject">✕ 驳回</button>
        </div>
      </div>
    </div>

    <!-- 稿件列表 -->
    <div class="card" style="margin-top:16px">
      <h3>稿件列表</h3>
      <table class="table" v-if="list.length">
        <thead><tr><th>标题</th><th>相似度</th><th>状态</th><th>更新时间</th><th></th></tr></thead>
        <tbody>
          <tr v-for="d in list" :key="d.id">
            <td>{{ d.title }}</td>
            <td>{{ (d.similarity_score * 100).toFixed(0) }}%</td>
            <td><span class="tag" :class="statusTag(d.status)">{{ d.status }}</span></td>
            <td>{{ d.updated_at.slice(0, 16).replace('T', ' ') }}</td>
            <td><button class="btn small primary" @click="open(d)">审核 / 查看</button></td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">该状态下暂无稿件</div>
    </div>
  </div>
</template>
