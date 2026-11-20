<script setup>
// 知识库：导入区(拖拽/粘贴) + 条目列表 + 检索预览（PRD 页面 3）
import { onMounted, ref } from 'vue'
import { apiGet, apiPatch, apiPost, apiUpload } from '../api.js'

const chunks = ref([])
const total = ref(0)
const page = ref(1)
const sourceFilter = ref('')
const dragOver = ref(false)

const pasteForm = ref({ title: '', content: '', source_type: 'experience' })
const searchForm = ref({ query: '' })
const hits = ref([])
const searching = ref(false)

async function load() {
  const data = await apiGet(`/api/knowledge/chunks?page=${page.value}&page_size=30${sourceFilter.value ? `&source_type=${sourceFilter.value}` : ''}`)
  chunks.value = data.items
  total.value = data.total
}

async function onDrop(e) {
  dragOver.value = false
  const file = e.dataTransfer.files[0]
  if (!file) return
  await uploadFile(file)
}

async function onPick(e) {
  const file = e.target.files[0]
  if (file) await uploadFile(file)
  e.target.value = ''
}

async function uploadFile(file) {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('source_type', 'experience')
  const data = await apiUpload('/api/knowledge/import-file', fd)
  alert(`导入成功：${data.count} 个切片`)
  await load()
}

async function importText() {
  if (!pasteForm.value.content.trim()) return
  const data = await apiPost('/api/knowledge/import-text', pasteForm.value)
  pasteForm.value.content = ''
  pasteForm.value.title = ''
  alert(`导入成功：${data.count} 个切片`)
  await load()
}

async function toggle(c) {
  await apiPatch(`/api/knowledge/chunks/${c.id}/toggle`)
  await load()
}

async function search() {
  if (!searchForm.value.query.trim()) return
  searching.value = true
  try {
    hits.value = await apiPost('/api/knowledge/search', { query: searchForm.value.query, top_k: 5 })
  } finally {
    searching.value = false
  }
}

const SOURCE_LABELS = { experience: '人工经验', review: '周复盘', negative: '负样本' }

onMounted(load)
</script>

<template>
  <div style="display:grid;grid-template-columns:1fr 320px;gap:16px">
    <div>
      <!-- 导入区 -->
      <div class="card">
        <h3>导入知识</h3>
        <div
          style="border:2px dashed var(--border);border-radius:10px;padding:24px;text-align:center;cursor:pointer"
          :style="dragOver ? 'border-color:var(--primary);background:#fdf0f3' : ''"
          @dragover.prevent="dragOver = true"
          @dragleave="dragOver = false"
          @drop.prevent="onDrop"
          @click="$refs.fileInput.click()"
        >
          拖拽 txt / md / docx 文件到此处，或点击选择文件
          <input ref="fileInput" type="file" accept=".txt,.md,.docx" style="display:none" @change="onPick" />
        </div>
        <div style="margin-top:12px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
          <input type="text" v-model="pasteForm.title" placeholder="来源标题（可选）" />
          <select v-model="pasteForm.source_type">
            <option value="experience">人工经验</option>
            <option value="review">周复盘</option>
            <option value="negative">负样本</option>
          </select>
          <button class="btn primary" @click="importText">粘贴文本导入 ↓</button>
        </div>
        <textarea style="margin-top:8px" rows="4" v-model="pasteForm.content" placeholder="在此粘贴运营经验文本…"></textarea>
      </div>

      <!-- 条目列表 -->
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="margin:0">知识切片（{{ total }}）</h3>
          <select v-model="sourceFilter" style="width:140px" @change="load">
            <option value="">全部来源</option>
            <option value="experience">人工经验</option>
            <option value="review">周复盘</option>
            <option value="negative">负样本</option>
          </select>
        </div>
        <table class="table" v-if="chunks.length">
          <thead><tr><th>标题</th><th>来源</th><th>字数</th><th>入库时间</th><th>启用</th></tr></thead>
          <tbody>
            <tr v-for="c in chunks" :key="c.id">
              <td :title="c.content">{{ c.source_title }}</td>
              <td><span class="tag" :class="{ experience: 'info', review: 'ok', negative: 'err' }[c.source_type] || 'plain'">
                {{ SOURCE_LABELS[c.source_type] || c.source_type }}</span></td>
              <td>{{ c.char_count }}</td>
              <td>{{ c.created_at.slice(0, 16).replace('T', ' ') }}</td>
              <td>
                <button class="btn small" @click="toggle(c)">{{ c.enabled ? '✓ 启用中' : '已停用' }}</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">暂无知识切片，先在上方导入</div>
        <div style="margin-top:10px;text-align:center" v-if="total > 30">
          <button class="btn small" :disabled="page <= 1" @click="page--; load()">上一页</button>
          <span style="margin:0 10px">{{ page }} / {{ Math.ceil(total / 30) }}</span>
          <button class="btn small" :disabled="page >= Math.ceil(total / 30)" @click="page++; load()">下一页</button>
        </div>
      </div>
    </div>

    <!-- 右：检索预览 -->
    <div class="card" style="align-self:start;position:sticky;top:0">
      <h3>检索预览（BM25）</h3>
      <div style="display:flex;gap:8px">
        <input type="text" v-model="searchForm.query" placeholder="输入查询词…" @keyup.enter="search" />
        <button class="btn primary" :disabled="searching" @click="search">搜索</button>
      </div>
      <div style="margin-top:12px">
        <div v-for="h in hits" :key="h.id" style="padding:10px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <b style="font-size:13px">{{ h.source_title }}</b>
            <span class="tag ok">得分 {{ h.score }}</span>
          </div>
          <div style="font-size:13px;color:var(--text-sub)">{{ h.content.slice(0, 150) }}…</div>
        </div>
        <div v-if="!hits.length" class="empty">输入查询词预览检索命中</div>
      </div>
    </div>
  </div>
</template>
