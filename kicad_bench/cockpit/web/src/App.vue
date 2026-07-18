<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CircleAlert, GitBranch, Menu, PanelLeft, PlugZap, Save } from 'lucide-vue-next'
import { getJSON, setMutationToken, type Board, type LiveStatus } from './api'
import AppSidebar from './components/AppSidebar.vue'
import OverviewView from './views/OverviewView.vue'
import AuditView from './views/AuditView.vue'
import ReviewView from './views/ReviewView.vue'
import SchematicView from './views/SchematicView.vue'
import Pcb2dView from './views/Pcb2dView.vue'
import Pcb3dView from './views/Pcb3dView.vue'
import PartsView from './views/PartsView.vue'
import DatasheetsView from './views/DatasheetsView.vue'
import StageView from './views/StageView.vue'
import ReleaseView from './views/ReleaseView.vue'

const product = ref<any>(null)
const boards = ref<Board[]>([])
const activeBoard = ref(localStorage.getItem('cockpit:board') || '')
const activeView = ref(localStorage.getItem('cockpit:view') || 'overview')
const status = ref<LiveStatus | null>(null)
const connected = ref(false)
const sidebarOpen = ref(false)
const error = ref('')
const tick = ref(0)
let events: EventSource | null = null

const views: Record<string, any> = {
  overview: OverviewView, audit: AuditView, review: ReviewView,
  schematic: SchematicView, pcb2d: Pcb2dView, pcb3d: Pcb3dView,
  parts: PartsView, datasheets: DatasheetsView, stage: StageView, release: ReleaseView,
}
const currentView = computed(() => views[activeView.value] || OverviewView)
const currentBoard = computed(() => boards.value.find((b) => b.id === activeBoard.value))
const auditClass = computed(() => {
  const state = status.value?.audit
  if (!state || state.status === 'none') return 'neutral'
  if (state.status === 'running' || state.status === 'stale') return 'warn'
  return state.verdict?.err_checks ? 'error' : 'ok'
})
const auditLabel = computed(() => {
  const a = status.value?.audit
  if (!a || a.status === 'none') return 'Not audited'
  if (a.status === 'running') return 'Audit running'
  if (a.status === 'stale') return 'Audit stale'
  return a.verdict?.err_checks ? `${a.verdict.err_checks} checks failing` : 'Audit clean'
})
const savedAt = computed(() => {
  const m = Math.max(status.value?.pcb_mtime || 0, status.value?.sch_mtime || 0)
  return m ? new Date(m * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'No files'
})

function connect() {
  events?.close()
  connected.value = false
  status.value = null
  if (!activeBoard.value) return
  events = new EventSource(`/api/events?board=${encodeURIComponent(activeBoard.value)}`)
  events.addEventListener('status', (event) => {
    status.value = JSON.parse((event as MessageEvent).data)
    connected.value = true
    tick.value++
  })
  events.onerror = () => { connected.value = false }
}

function chooseBoard(id: string) {
  activeBoard.value = id
  localStorage.setItem('cockpit:board', id)
}
function chooseView(id: string) {
  activeView.value = id
  localStorage.setItem('cockpit:view', id)
}

watch(activeBoard, connect)
onMounted(async () => {
  try {
    const [p, b] = await Promise.all([
      getJSON<any>('/api/product'), getJSON<{ items: Board[] }>('/api/boards'),
    ])
    product.value = p
    setMutationToken(p.mutation_token || '')
    boards.value = b.items
    if (!b.items.some((item) => item.id === activeBoard.value)) activeBoard.value = b.items[0]?.id || ''
    connect()
  } catch (e: any) { error.value = e.message }
})
onBeforeUnmount(() => events?.close())
</script>

<template>
  <div class="app-shell">
    <AppSidebar
      :open="sidebarOpen" :product="product" :boards="boards"
      :active-board="activeBoard" :active-view="activeView"
      @close="sidebarOpen = false" @board="chooseBoard" @view="chooseView"
    />

    <section class="app-main">
      <header class="topbar">
        <button class="icon-btn sidebar-toggle" title="Open navigation" @click="sidebarOpen = true">
          <Menu :size="20" />
        </button>
        <div class="board-title">
          <strong>{{ currentBoard?.name || 'No board' }}</strong>
          <span>{{ currentBoard?.pcb || currentBoard?.schematic || 'Choose a board' }}</span>
        </div>
        <div class="status-strip">
          <button class="status-chip" :class="auditClass" @click="chooseView('audit')">
            <CircleAlert :size="14" /> {{ auditLabel }}
          </button>
          <span class="status-item" :class="{ warn: status?.stage?.open }">
            <PlugZap :size="14" /> KiCad {{ status?.stage?.open ? 'open' : 'closed' }}
          </span>
          <span class="status-item"><Save :size="14" /> {{ savedAt }}</span>
          <span class="status-item"><GitBranch :size="14" /> {{ status?.git?.branch || 'no git' }}</span>
          <span class="connection-dot" :class="{ connected }" :title="connected ? 'Live updates connected' : 'Reconnecting' "></span>
        </div>
      </header>

      <main class="workspace">
        <div v-if="error" class="error-banner">{{ error }}</div>
        <component
          v-else-if="activeBoard" :is="currentView" :board="activeBoard"
          :board-info="currentBoard" :status="status" :tick="tick"
          @navigate="chooseView"
        />
        <div v-else class="empty-state"><PanelLeft :size="28" /> No boards are configured.</div>
      </main>
    </section>
  </div>
</template>
