<script setup>
// 应用壳：左侧导航 + 顶部状态条 + 动态页面切换
import { computed } from 'vue'
import Layout from './components/Layout.vue'
import { store } from './store.js'

import Dashboard from './pages/Dashboard.vue'
import Account from './pages/Account.vue'
import Personas from './pages/Personas.vue'
import Knowledge from './pages/Knowledge.vue'
import Collect from './pages/Collect.vue'
import Review from './pages/Review.vue'
import PublishCalendar from './pages/PublishCalendar.vue'
import Stats from './pages/Stats.vue'
import Reports from './pages/Reports.vue'

const pageMap = {
  dashboard: Dashboard,
  account: Account,
  personas: Personas,
  knowledge: Knowledge,
  collect: Collect,
  review: Review,
  publish: PublishCalendar,
  stats: Stats,
  reports: Reports,
}

const currentPage = computed(() => pageMap[store.page] || Dashboard)
</script>

<template>
  <Layout>
    <component :is="currentPage" />
  </Layout>
</template>
