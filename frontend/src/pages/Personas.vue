<script setup>
// 人设管理：左侧卡片列表 + 右侧编辑表单 + 生效开关（PRD 页面 2）
import { onMounted, ref } from 'vue'
import { apiDelete, apiGet, apiPost, apiPut } from '../api.js'
import { store } from '../store.js'

const personas = ref([])
const editing = ref(null)          // 当前编辑对象（null=新建）
const form = ref({ name: '', nickname: '', positioning: '', style_tags: '', audience: '', taboo_words: '', sample_notes: '' })
const saving = ref(false)

const STYLE_PRESETS = ['好物分享', '干货教程', '生活记录', '测评种草', '治愈系', '搞笑', '知识科普', '穿搭美妆']

async function load() {
  personas.value = await apiGet('/api/personas')
  const active = personas.value.find((p) => p.is_active)
  if (active) {
    store.personaName = active.name
    store.personaId = active.id
  }
}

function openNew() {
  editing.value = {}
  form.value = { name: '', nickname: '', positioning: '', style_tags: '', audience: '', taboo_words: '', sample_notes: '' }
}

function openEdit(p) {
  editing.value = p
  form.value = {
    name: p.name,
    nickname: p.nickname,
    positioning: p.positioning,
    style_tags: (p.style_tags || []).join('、'),
    audience: p.audience,
    taboo_words: (p.taboo_words || []).join('、'),
    sample_notes: (p.sample_notes || []).join('\n---\n'),
  }
}

function toggleStyle(tag) {
  const arr = form.value.style_tags.split('、').filter((x) => x.trim())
  if (arr.includes(tag)) form.value.style_tags = arr.filter((x) => x !== tag).join('、')
  else arr.push(tag)
  form.value.style_tags = arr.join('、')
}

async function save() {
  if (!form.value.name.trim()) return
  saving.value = true
  try {
    const payload = {
      name: form.value.name,
      nickname: form.value.nickname,
      positioning: form.value.positioning,
      style_tags: form.value.style_tags.split('、').map((x) => x.trim()).filter(Boolean),
      audience: form.value.audience,
      taboo_words: form.value.taboo_words.split('、').map((x) => x.trim()).filter(Boolean),
      sample_notes: form.value.sample_notes.split('\n---\n').map((x) => x.trim()).filter(Boolean),
    }
    if (editing.value && editing.value.id) {
      await apiPut(`/api/personas/${editing.value.id}`, payload)
    } else {
      await apiPost('/api/personas', payload)
    }
    editing.value = null
    await load()
  } finally {
    saving.value = false
  }
}

async function activate(p) {
  await apiPost(`/api/personas/${p.id}/activate`)
  await load()
}

async function remove(p) {
  if (!confirm(`确认删除人设「${p.name}」？`)) return
  await apiDelete(`/api/personas/${p.id}`)
  await load()
}

onMounted(load)
</script>

<template>
  <div class="grid-2" style="grid-template-columns: 360px 1fr">
    <!-- 左：人设卡片列表 -->
    <div>
      <button class="btn primary" style="width:100%;margin-bottom:12px" @click="openNew">＋ 新建人设</button>
      <div v-for="p in personas" :key="p.id" class="card" style="cursor:pointer" @click="openEdit(p)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <b>{{ p.name }}</b>
          <span class="tag" :class="p.is_active ? 'ok' : 'plain'">{{ p.is_active ? '生效中' : '未启用' }}</span>
        </div>
        <div style="color:var(--text-sub);margin:6px 0;font-size:13px">{{ p.positioning || p.nickname || '（未填定位）' }}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <span v-for="t in p.style_tags" :key="t" class="tag info">{{ t }}</span>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px" @click.stop>
          <button v-if="!p.is_active" class="btn small primary" @click="activate(p)">设为生效</button>
          <button class="btn small danger" @click="remove(p)">删除</button>
        </div>
      </div>
      <div v-if="!personas.length" class="empty">还没有人设，点击上方「新建人设」开始</div>
    </div>

    <!-- 右：编辑表单 -->
    <div class="card" v-if="editing">
      <h3>{{ editing.id ? `编辑人设：${editing.name}` : '新建人设' }}</h3>
      <div class="field"><label>人设名 *</label><input type="text" v-model="form.name" placeholder="如：家居好物分享官" /></div>
      <div class="field"><label>账号昵称定位</label><input type="text" v-model="form.nickname" placeholder="如：小屋酱" /></div>
      <div class="field"><label>一句话定位</label><input type="text" v-model="form.positioning" placeholder="如：帮打工人把出租屋住出幸福感" /></div>
      <div class="field">
        <label>风格标签（点选 + 自定义）</label>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
          <span v-for="t in STYLE_PRESETS" :key="t" class="tag" :class="form.style_tags.includes(t) ? 'ok' : 'plain'"
                style="cursor:pointer" @click="toggleStyle(t)">{{ t }}</span>
        </div>
        <input type="text" v-model="form.style_tags" placeholder="顿号分隔，如：好物分享、治愈系" />
      </div>
      <div class="field"><label>目标人群</label><input type="text" v-model="form.audience" placeholder="如：20-30 岁都市租房青年" /></div>
      <div class="field"><label>禁忌词（顿号分隔，命中不入选）</label><input type="text" v-model="form.taboo_words" placeholder="如：最便宜、绝对、加微信" /></div>
      <div class="field">
        <label>示例笔记（多条用 --- 分行隔开）</label>
        <textarea v-model="form.sample_notes" rows="6" placeholder="示例1&#10;---&#10;示例2"></textarea>
      </div>
      <div style="display:flex;gap:10px">
        <button class="btn primary" :disabled="saving" @click="save">保存</button>
        <button class="btn" @click="editing = null">取消</button>
      </div>
    </div>
    <div v-else class="card"><div class="empty">从左侧选择人设进行编辑</div></div>
  </div>
</template>
