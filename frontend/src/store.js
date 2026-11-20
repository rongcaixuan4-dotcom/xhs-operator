// 响应式全局状态：当前页面、当前人设、流水线状态灯、AI 连通状态、告警、toast
import { reactive } from 'vue'

export const store = reactive({
  /** 当前页面 key（左侧导航切换） */
  page: 'dashboard',

  /** 当前生效人设（Dashboard/设置页更新，顶部状态条展示） */
  personaName: '',
  personaId: null,

  /** 流水线状态灯：{ stage, status }，stage 见 PipelineRun.stage 枚举 */
  pipeline: { stage: 'idle', status: 'idle', updatedAt: '' },

  /** AI 连通状态：{ llm: 'unknown'|'ok'|'fail', image: 'unknown'|'ok'|'fail' } */
  aiStatus: { llm: 'unknown', image: 'unknown' },

  /** 今日发布数 */
  todayPublishCount: 0,

  /** 告警列表（[{level, message, time}]） */
  alerts: [],

  /** toast 队列 */
  toasts: [],

  pushToast(message, type = 'info') {
    const id = Date.now() + Math.random()
    this.toasts.push({ id, message, type })
    setTimeout(() => {
      this.toasts = this.toasts.filter((t) => t.id !== id)
    }, 4000)
  },
})

export const NAV_ITEMS = [
  { key: 'dashboard', label: '工作台', icon: '📊' },
  { key: 'account', label: '账号登录', icon: '🔑' },
  { key: 'personas', label: '人设管理', icon: '👤' },
  { key: 'knowledge', label: '知识库', icon: '📚' },
  { key: 'collect', label: '采集与选题', icon: '🔍' },
  { key: 'review', label: '二创审核', icon: '✍️' },
  { key: 'publish', label: '发布日历', icon: '📅' },
  { key: 'stats', label: '数据看板', icon: '📈' },
  { key: 'reports', label: '复盘', icon: '🔁' },
]
