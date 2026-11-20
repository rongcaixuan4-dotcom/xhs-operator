<script setup>
// 账号登录页：登录态查询、发起扫码登录（轮询直到成功）、关闭登录窗口
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiGet, apiPost } from '../api.js'
import { store } from '../store.js'

const status = ref({ logged_in: false, message: '尚未检查' })
const checking = ref(false)
const starting = ref(false)
const polling = ref(false)

let pollTimer = null

const statusClass = computed(() =>
  status.value.logged_in ? 'ok' : 'err',
)

async function refreshStatus(silent = true) {
  checking.value = true
  try {
    const data = await apiGet('/api/account/status')
    const before = status.value.logged_in
    status.value = { logged_in: !!data.logged_in, message: data.message || '' }
    // 扫码成功瞬间：停止轮询并提示
    if (!before && status.value.logged_in && polling.value) {
      stopPolling()
      store.pushToast('🎉 小红书登录成功！采集与发布通道已就绪', 'ok')
    }
  } catch (e) {
    if (!silent) status.value = { logged_in: false, message: e.message }
  } finally {
    checking.value = false
  }
}

function startPolling() {
  stopPolling()
  polling.value = true
  pollTimer = setInterval(() => refreshStatus(true), 4000)
}

function stopPolling() {
  polling.value = false
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function startLogin() {
  starting.value = true
  try {
    const data = await apiPost('/api/account/login/start')
    store.pushToast(data.message || '浏览器窗口已打开，请扫码', 'info')
    if (data.started) startPolling()
  } catch (e) {
    /* toast 已由 api.js 弹出 */
  } finally {
    starting.value = false
  }
}

async function closeLoginWindow() {
  await apiPost('/api/account/login/close')
  store.pushToast('登录窗口已关闭（已登录的态不受影响）', 'info')
  stopPolling()
}

onMounted(() => refreshStatus(true))
onUnmounted(() => stopPolling())
</script>

<template>
  <div class="page-wrap">
    <section class="card">
      <h2>🔑 小红书账号</h2>
      <p class="hint">
        系统使用持久化浏览器（独立 profile）保存登录态：<b>登录一次长期有效</b>，
        采集、二创发布、数据快照全部复用该登录态；API 直连采集通道也会自动读取这里导出的 Cookie。
      </p>

      <div class="status-row">
        <span class="big-badge" :class="statusClass">
          {{ status.logged_in ? '✓ 已登录' : '✕ 未登录' }}
        </span>
        <span class="status-msg">{{ status.message }}</span>
      </div>

      <div class="row-btns">
        <button class="btn primary" :disabled="starting" @click="startLogin">
          {{ starting ? '正在打开浏览器…' : '打开浏览器扫码登录' }}
        </button>
        <button class="btn" :disabled="checking" @click="refreshStatus(false)">刷新状态</button>
        <button class="btn ghost" @click="closeLoginWindow">关闭登录窗口</button>
      </div>

      <div v-if="polling" class="poll-tip">
        <span class="dot ok"></span> 正在等待扫码确认…（每 4 秒自动检查一次，扫码成功会自动提示）
      </div>

      <ol class="steps">
        <li>点击「打开浏览器扫码登录」——会弹出一个独立的 Chromium 窗口并打开小红书首页</li>
        <li>在弹出的窗口里点击右上角「登录」，用小红书 App 扫二维码并确认</li>
        <li>看到「✓ 已登录」即可关闭窗口，去 <b>人设管理</b> 建人设、<b>采集与选题</b> 配话题</li>
      </ol>
    </section>
  </div>
</template>

<style scoped>
.page-wrap { max-width: 720px; }
.status-row { display: flex; align-items: center; gap: 12px; margin: 14px 0 6px; }
.big-badge { font-size: 15px; font-weight: 700; padding: 6px 14px; border-radius: 8px; }
.big-badge.ok { background: rgba(46, 160, 67, 0.12); color: #1a7f37; }
.big-badge.err { background: rgba(207, 34, 46, 0.10); color: var(--err, #cf222e); }
.status-msg { color: var(--text-2, #555); }
.poll-tip { display: flex; align-items: center; gap: 8px; margin-top: 10px; color: var(--text-2, #555); font-size: 13px; }
.steps { margin-top: 16px; padding-left: 20px; line-height: 1.9; color: var(--text-2, #555); }
.row-btns { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
</style>
