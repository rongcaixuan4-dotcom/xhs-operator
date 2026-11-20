<script setup>
// 布局壳：左侧 8 项导航 + 顶部状态条（当前人设/流水线灯/AI 连通/今日发布数）+ toast
import { onMounted } from 'vue'
import { store, NAV_ITEMS } from '../store.js'
import { apiGet } from '../api.js'

function go(key) {
  store.page = key
}

async function refreshStatus() {
  try {
    // 顶部状态条聚合自工作台 summary（当前人设/流水线/今日发布/告警）
    const data = await apiGet('/api/dashboard/summary')
    if (data && data.persona) {
      store.personaName = data.persona.name || ''
      store.personaId = data.persona.id || null
    }
    if (data && data.pipeline) {
      store.pipeline = data.pipeline
    }
    if (data && typeof data.today_publish_count === 'number') {
      store.todayPublishCount = data.today_publish_count
    }
    if (data && Array.isArray(data.alerts)) {
      store.alerts = data.alerts
    }
    if (data && data.ai_status) {
      store.aiStatus = data.ai_status
    }
  } catch (e) {
    /* 静默：toast 已由 api.js 处理，避免轮询刷屏 */
  }
}

onMounted(() => {
  refreshStatus()
  setInterval(refreshStatus, 15000) // 15 秒轮询一次状态灯
})
</script>

<template>
  <div class="app-shell">
    <!-- 左侧导航 -->
    <aside class="sidebar">
      <div class="logo">📕 XHS 运营助手</div>
      <div
        v-for="item in NAV_ITEMS"
        :key="item.key"
        class="nav-item"
        :class="{ active: store.page === item.key }"
        @click="go(item.key)"
      >
        <span>{{ item.icon }}</span><span>{{ item.label }}</span>
      </div>
    </aside>

    <!-- 右列 -->
    <div class="main-col">
      <!-- 顶部状态条 -->
      <div class="topbar">
        <span class="chip">👤 当前人设：<b>{{ store.personaName || '未设置' }}</b></span>
        <span class="chip">
          <span class="dot" :class="store.pipeline.status"></span>
          流水线：{{ store.pipeline.stage }} / {{ store.pipeline.status }}
        </span>
        <span class="chip">
          AI 文案
          <span class="dot" :class="store.aiStatus.llm === 'ok' ? 'ok' : (store.aiStatus.llm === 'fail' ? 'err' : 'idle')"></span>
          AI 生图
          <span class="dot" :class="store.aiStatus.image === 'ok' ? 'ok' : (store.aiStatus.image === 'fail' ? 'err' : 'idle')"></span>
        </span>
        <span class="chip">📅 今日发布：<b>{{ store.todayPublishCount }}</b></span>
        <span class="chip" v-if="store.alerts.length" style="color: var(--err)">
          ⚠ 告警 {{ store.alerts.length }} 条
        </span>
      </div>

      <!-- 页面内容 -->
      <div class="page"><slot /></div>
    </div>

    <!-- toast 容器 -->
    <div class="toasts">
      <div v-for="t in store.toasts" :key="t.id" class="toast" :class="t.type">{{ t.message }}</div>
    </div>
  </div>
</template>
