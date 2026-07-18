<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Activity, ArrowRight, CheckCircle2, Clock3, FileWarning, GitCommit, Layers3, PlayCircle } from 'lucide-vue-next'
import { boardUrl, getJSON, postJSON } from '../api'

const props = defineProps<{ board: string, boardInfo: any, status: any, tick: number }>()
const emit = defineEmits(['navigate'])
const changes = ref<any>({ commits: [], last_diff: '' })
const busy = ref(false)

const audit = computed(() => props.status?.audit || { status: 'none' })
const counts = computed(() => audit.value.verdict || { err_checks: 0, warn_checks: 0, clean: 0, total: 0 })
const readiness = computed(() => props.status?.release || { status: 'none' })
const queue = computed(() => props.status?.stage?.jobs?.length || 0)

async function loadChanges() {
  try { changes.value = await getJSON(boardUrl(props.board, 'changes')) } catch { changes.value = { commits: [], last_diff: '' } }
}
async function runAudit() {
  busy.value = true
  try { await postJSON(boardUrl(props.board, 'audit')) } finally { busy.value = false }
  emit('navigate', 'audit')
}
watch(() => props.board, loadChanges)
onMounted(loadChanges)
</script>

<template>
  <div class="view-scroll overview-view">
    <div class="view-heading">
      <div><span class="eyebrow">Board workspace</span><h2>{{ board }}</h2></div>
      <button class="btn primary" :disabled="busy" @click="runAudit"><PlayCircle :size="16" /> Run audit</button>
    </div>

    <section class="metric-grid">
      <button class="metric" @click="emit('navigate', 'audit')">
        <span class="metric-icon" :class="counts.err_checks ? 'error' : 'ok'"><Activity :size="18" /></span>
        <span><small>Audit</small><strong>{{ counts.err_checks ? `${counts.err_checks} failing` : audit.status === 'ready' ? 'Clean' : audit.status }}</strong></span>
        <ArrowRight :size="16" />
      </button>
      <button class="metric" @click="emit('navigate', 'release')">
        <span class="metric-icon" :class="readiness.ready ? 'ok' : 'warn'"><CheckCircle2 :size="18" /></span>
        <span><small>Release</small><strong>{{ readiness.status === 'ready' ? (readiness.ready ? 'Ready' : 'Blocked') : 'Not checked' }}</strong></span>
        <ArrowRight :size="16" />
      </button>
      <button class="metric" @click="emit('navigate', 'stage')">
        <span class="metric-icon" :class="queue ? 'warn' : 'neutral'"><Clock3 :size="18" /></span>
        <span><small>Stage queue</small><strong>{{ queue }} queued</strong></span>
        <ArrowRight :size="16" />
      </button>
      <button class="metric" @click="emit('navigate', 'pcb2d')">
        <span class="metric-icon neutral"><Layers3 :size="18" /></span>
        <span><small>Board files</small><strong>{{ boardInfo?.has_pcb ? 'PCB + schematic' : 'Schematic only' }}</strong></span>
        <ArrowRight :size="16" />
      </button>
    </section>

    <div class="overview-columns">
      <section class="section-block">
        <div class="section-heading"><div><span class="eyebrow">Quality snapshot</span><h3>Current checks</h3></div></div>
        <div v-if="audit.status === 'none'" class="empty-inline"><FileWarning :size="18" /> Run the audit to establish a baseline.</div>
        <template v-else>
          <div class="quality-bar" :aria-label="`${counts.clean} clean, ${counts.warn_checks} warning, ${counts.err_checks} failing`">
            <span class="clean" :style="{ flex: counts.clean || 0.001 }"></span>
            <span class="warning" :style="{ flex: counts.warn_checks || 0.001 }"></span>
            <span class="failing" :style="{ flex: counts.err_checks || 0.001 }"></span>
          </div>
          <div class="quality-legend">
            <span><i class="dot ok"></i>{{ counts.clean }} clean</span>
            <span><i class="dot warn"></i>{{ counts.warn_checks }} warning</span>
            <span><i class="dot error"></i>{{ counts.err_checks }} failing</span>
          </div>
        </template>
        <div class="file-roster">
          <div><span>Schematic</span><strong>{{ boardInfo?.schematic || 'Not configured' }}</strong></div>
          <div><span>PCB</span><strong>{{ boardInfo?.pcb || 'Not configured' }}</strong></div>
          <div><span>Git</span><strong>{{ status?.git?.dirty ? `${status.git.dirty} changed files` : 'Working tree clean' }}</strong></div>
        </div>
      </section>

      <section class="section-block">
        <div class="section-heading"><div><span class="eyebrow">Repository history</span><h3>Recent changes</h3></div></div>
        <div v-if="!changes.commits?.length" class="empty-inline">No Git history available.</div>
        <div v-for="commit in changes.commits?.slice(0, 6)" :key="commit.hash" class="commit-row">
          <GitCommit :size="16" />
          <div><strong>{{ commit.subject }}</strong><span>{{ commit.hash }} · {{ commit.when }}</span></div>
        </div>
      </section>
    </div>
  </div>
</template>
