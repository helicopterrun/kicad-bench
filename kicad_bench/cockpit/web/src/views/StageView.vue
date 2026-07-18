<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { CircleStop, Clock3, History, LoaderCircle, Play, Plus, RotateCcw, Trash2 } from 'lucide-vue-next'
import { boardUrl, deleteJSON, getJSON, postJSON } from '../api'

const props = defineProps<{ board: string, status: any, tick: number }>()
const data = ref<any>({ jobs: [], hooks: [], last_run: null, open: false, locks: [] })
const label = ref('')
const executable = ref('')
const args = ref('')
const busy = ref(false)
const error = ref('')
const live = computed(() => props.status?.stage || data.value)

async function load() {
  try { data.value = await getJSON(boardUrl(props.board, 'stage')) } catch (e: any) { error.value = e.message }
}
async function add() {
  if (!label.value.trim() || !executable.value.trim()) return
  busy.value = true; error.value = ''
  const command = [executable.value.trim(), ...args.value.split('\n').map((v) => v.trim()).filter(Boolean)]
  try {
    data.value = await postJSON(boardUrl(props.board, 'stage/jobs'), { label: label.value.trim(), command })
    label.value = ''; executable.value = ''; args.value = ''
  } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
async function clear() {
  if (!confirm(`Clear all ${live.value.jobs?.length || 0} queued jobs?`)) return
  data.value = await deleteJSON(boardUrl(props.board, 'stage/jobs'))
}
async function runNow() {
  if (!confirm('Run every queued command now, ignoring the KiCad lock state?')) return
  busy.value = true
  try { data.value = await postJSON(boardUrl(props.board, 'stage/run')) } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
watch(() => props.board, load)
onMounted(load)
</script>

<template>
  <div class="view-scroll">
    <div class="view-heading">
      <div><span class="eyebrow">Lock-aware automation</span><h2>Stage</h2><p>Queue local work until KiCad releases the project files.</p></div>
      <div class="stage-state" :class="live.open ? 'warn' : 'ok'"><CircleStop v-if="live.open" :size="18" /><Play v-else :size="18" /><div><strong>KiCad {{ live.open ? 'open' : 'closed' }}</strong><span>{{ live.locks?.length || 0 }} project locks</span></div></div>
    </div>
    <div v-if="error" class="error-banner">{{ error }}</div>

    <div class="stage-layout">
      <section class="section-block stage-form">
        <div class="section-heading"><div><span class="eyebrow">One-shot work</span><h3>Add a job</h3></div><Plus :size="18" /></div>
        <label><span>Label</span><input v-model="label" placeholder="Rebuild touch sheet" /></label>
        <label><span>Executable</span><input v-model="executable" class="mono" placeholder="python3" /></label>
        <label><span>Arguments</span><textarea v-model="args" class="mono" rows="5" placeholder="scripts/build_touch_sheet.py&#10;--check"></textarea><small>One argument per line. Jobs run from the project root.</small></label>
        <button class="btn primary full" :disabled="busy || !label || !executable" @click="add"><LoaderCircle v-if="busy" class="spin" :size="16" /><Clock3 v-else :size="16" /> Add to queue</button>
      </section>

      <section class="section-block queue-panel">
        <div class="section-heading"><div><span class="eyebrow">Pending</span><h3>Queue</h3></div><span class="count-badge">{{ live.jobs?.length || 0 }}</span></div>
        <div v-if="!live.jobs?.length" class="empty-inline"><Clock3 :size="18" /> No staged jobs.</div>
        <div v-for="job in live.jobs" :key="job.id" class="job-row">
          <Clock3 :size="16" /><div><strong>{{ job.label }}</strong><code>{{ job.command?.join(' ') || job.cmd?.join(' ') }}</code><span>{{ job.added }}</span></div>
        </div>
        <div class="queue-actions"><button class="btn danger" :disabled="!live.jobs?.length" @click="clear"><Trash2 :size="15" /> Clear</button><button class="btn primary" :disabled="busy || !live.jobs?.length" @click="runNow"><Play :size="15" /> Run now</button></div>
      </section>
    </div>

    <section class="section-block history-panel">
      <div class="section-heading"><div><span class="eyebrow">Automation</span><h3>Recurring hooks and last run</h3></div><History :size="18" /></div>
      <div class="history-columns">
        <div><h4>On-close hooks</h4><div v-if="!live.hooks?.length" class="empty-inline">No recurring hooks configured.</div><div v-for="hook in live.hooks" :key="hook.id" class="job-row"><RotateCcw :size="15" /><div><strong>{{ hook.label }}</strong><code>{{ hook.cmd?.join(' ') }}</code></div></div></div>
        <div><h4>Last run</h4><div v-if="!live.last_run" class="empty-inline">No completed jobs yet.</div><template v-else><span class="muted-copy">{{ live.last_run.finished }}</span><div v-for="(result, index) in live.last_run.results" :key="index" class="result-row" :class="result.ok ? 'ok' : 'error'"><strong>{{ result.ok ? 'PASS' : 'FAIL' }}</strong><span>{{ result.label }}</span></div></template></div>
      </div>
    </section>
  </div>
</template>
