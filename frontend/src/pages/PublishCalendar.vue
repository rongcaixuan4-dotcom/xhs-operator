<script setup>
// 发布日历：月历视图 + 发布配置抽屉（全配置项）+ 试发预检（PRD 页面 6）
import { computed, onMounted, ref } from 'vue'
import { apiDelete, apiGet, apiPost, apiPut, fileUrl } from '../api.js'

const month = ref(new Date().toISOString().slice(0, 7))
const days = ref({})
const tasks = ref([])
const drawer = ref(false)
const precheckResult = ref(null)

// 抽屉表单（字段与创作者服务中心一致）
const form = ref({
  draft_id: null,
  scheduled_at: '',
  topics: '',
  location: '',
  source_declare: false,
  collection: '',
  mention_users: '',
  is_schedule: true,
})

async function load() {
  const cal = await apiGet(`/api/publish/calendar?month=${month.value}`)
  days.value = cal.days || {}
  const data = await apiGet('/api/publish/tasks?page_size=100')
  tasks.value = data.items
}

function shiftMonth(delta) {
  const [y, m] = month.value.split('-').map(Number)
  const d = new Date(y, m - 1 + delta, 1)
  month.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
  load()
}

const daysInMonth = computed(() => {
  const [y, m] = month.value.split('-').map(Number)
  return new Date(y, m, 0).getDate()
})

const drafts = ref([])
async function openDrawer(taskId) {
  precheckResult.value = null
  const data = await apiGet('/api/creations?page_size=100')
  drafts.value = data.items.filter((d) => d.status === 'approved' || d.status === 'pending_review')
  if (taskId) {
    const t = tasks.value.find((x) => x.id === taskId)
    if (t) {
      form.value = {
        draft_id: t.draft_id,
        scheduled_at: (t.scheduled_at || '').slice(0, 16),
        topics: (t.publish_config.topics || []).join('、'),
        location: t.publish_config.location || '',
        source_declare: !!t.publish_config.source_declare,
        collection: t.publish_config.collection || '',
        mention_users: (t.publish_config.mention_users || []).join('、'),
        is_schedule: !!t.publish_config.is_schedule,
      }
      form.value._id = t.id
    }
  } else {
    form.value = { draft_id: null, scheduled_at: '', topics: '', location: '', source_declare: false, collection: '', mention_users: '', is_schedule: true }
    form.value._id = null
  }
  drawer.value = true
}

async function saveTask() {
  const payload = {
    draft_id: form.value.draft_id,
    scheduled_at: form.value.scheduled_at ? new Date(form.value.scheduled_at).toISOString() : null,
    topics: form.value.topics.split(/[、,，\s]+/).filter(Boolean),
    location: form.value.location,
    source_declare: form.value.source_declare,
    collection: form.value.collection,
    mention_users: form.value.mention_users.split(/[、,，\s]+/).filter(Boolean),
    is_schedule: form.value.is_schedule,
  }
  if (form.value._id) await apiPut(`/api/publish/tasks/${form.value._id}`, payload)
  else await apiPost('/api/publish/tasks', payload)
  drawer.value = false
  await load()
}

async function removeTask(taskId) {
  if (!confirm('取消该发布任务？')) return
  await apiDelete(`/api/publish/tasks/${taskId}`)
  drawer.value = false
  await load()
}

async function retryTask(taskId) {
  await apiPost(`/api/publish/tasks/${taskId}/retry`)
  await load()
}

async function runPrecheck() {
  precheckResult.value = await apiPost('/api/publish/precheck')
}

function statusTag(s) {
  return { queued: 'info', publishing: 'warn', verifying: 'warn', done: 'ok', failed: 'err', canceled: 'plain' }[s] || 'plain'
}

onMounted(load)
</script>

<template>
  <div class="grid-2" style="grid-template-columns: 1fr 380px">
    <!-- 月历 -->
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <button class="btn small" @click="shiftMonth(-1)">‹ 上月</button>
        <b style="font-size:16px">{{ month }}</b>
        <button class="btn small" @click="shiftMonth(1)">下月 ›</button>
      </div>
      <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px">
        <div v-for="w in ['一','二','三','四','五','六','日']" :key="w" style="text-align:center;color:var(--text-sub);font-size:12px">{{ w }}</div>
        <div v-for="i in daysInMonth" :key="i"
             style="min-height:80px;border:1px solid var(--border);border-radius:6px;padding:4px;font-size:12px">
          <div style="color:var(--text-sub)">{{ i }}</div>
          <div v-for="t in (days[`${month}-${String(i).padStart(2, '0')}`] || [])" :key="t.id"
               style="margin:2px 0;padding:2px 4px;border-radius:4px;cursor:pointer;font-size:11px"
               :style="{ background: t.status === 'done' ? '#e7f7ee' : t.status === 'failed' ? '#fee2e2' : '#e8f0fe' }"
               @click="openDrawer(t.id)">
            {{ t.scheduled_at?.slice(11, 16) }} {{ t.title.slice(0, 8) }}
          </div>
        </div>
      </div>
      <div style="margin-top:10px;display:flex;gap:10px;font-size:12px;color:var(--text-sub)">
        <span><span style="display:inline-block;width:10px;height:10px;background:#e8f0fe;border-radius:2px"></span> 待发布</span>
        <span><span style="display:inline-block;width:10px;height:10px;background:#e7f7ee;border-radius:2px"></span> 已发布</span>
        <span><span style="display:inline-block;width:10px;height:10px;background:#fee2e2;border-radius:2px"></span> 失败</span>
        <button class="btn small primary" style="margin-left:auto" @click="openDrawer(null)">＋ 新建发布任务</button>
      </div>
    </div>

    <!-- 任务列表 -->
    <div class="card">
      <h3>任务列表</h3>
      <div v-for="t in tasks.slice(0, 15)" :key="t.id" style="padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between">
          <b style="font-size:13px">{{ t.publish_config.title }}</b>
          <span class="tag" :class="statusTag(t.status)">{{ t.status }}</span>
        </div>
        <div style="font-size:12px;color:var(--text-sub);margin-top:4px">
          排期：{{ t.scheduled_at?.slice(0, 16).replace('T', ' ') || '-' }} · 重试 {{ t.retry_count }}
        </div>
        <div v-if="t.last_error" style="font-size:12px;color:var(--err)">{{ t.last_error }}</div>
        <div style="margin-top:6px;display:flex;gap:6px">
          <button class="btn small" @click="openDrawer(t.id)">编辑</button>
          <button v-if="t.status === 'failed'" class="btn small primary" @click="retryTask(t.id)">重试</button>
          <button v-if="t.status !== 'done'" class="btn small danger" @click="removeTask(t.id)">取消</button>
        </div>
      </div>
      <div v-if="!tasks.length" class="empty">暂无发布任务</div>
    </div>

    <!-- 发布配置抽屉 -->
    <teleport to="body">
      <div v-if="drawer">
        <div class="drawer-mask" @click="drawer = false"></div>
        <div class="drawer">
          <h3>{{ form._id ? '编辑发布任务' : '新建发布任务' }}</h3>
          <div class="field"><label>二创稿 *</label>
            <select v-model="form.draft_id">
              <option :value="null" disabled>选择已通过/待审的二创稿</option>
              <option v-for="d in drafts" :key="d.id" :value="d.id">{{ d.title }}（{{ d.status }}）</option>
            </select>
          </div>
          <div class="field"><label>定时发布时间（不填则自动取下一个发布时段）</label>
            <input type="datetime-local" v-model="form.scheduled_at" />
          </div>
          <div class="field"><label>话题标签（顿号分隔）</label><input type="text" v-model="form.topics" /></div>
          <div class="field"><label>位置</label><input type="text" v-model="form.location" /></div>
          <div class="field"><label><input type="checkbox" v-model="form.source_declare" style="width:auto;margin-right:6px" />声明来源</label></div>
          <div class="field"><label>合集</label><input type="text" v-model="form.collection" placeholder="合集名（依赖账号已有合集）" /></div>
          <div class="field"><label>@好友（顿号分隔昵称）</label><input type="text" v-model="form.mention_users" /></div>
          <div class="field"><label><input type="checkbox" v-model="form.is_schedule" style="width:auto;margin-right:6px" />定时发布（否则按排期时间立即发）</label></div>

          <button class="btn" style="width:100%" @click="runPrecheck">🧪 立即试发预检（不提交）</button>
          <div v-if="precheckResult" style="margin-top:8px;padding:8px;border-radius:8px;font-size:13px"
               :style="{ background: precheckResult.ok ? '#e7f7ee' : '#fee2e2' }">
            {{ precheckResult.ok ? '✓ 预检通过：登录态正常，发布页可达' : `✕ 预检未通过：${precheckResult.detail}` }}
          </div>

          <div style="display:flex;gap:10px;margin-top:14px">
            <button class="btn primary" style="flex:1" @click="saveTask">保存任务</button>
            <button class="btn" @click="drawer = false">取消</button>
          </div>
        </div>
      </div>
    </teleport>
  </div>
</template>
