<script setup lang="ts">
import { computed, ref } from 'vue'
import { CheckCircle2, ChevronDown, CircleAlert, Filter, LoaderCircle, Play, Search } from 'lucide-vue-next'
import { boardUrl, postJSON } from '../api'

const props = defineProps<{ board: string, status: any }>()
const query = ref('')
const issuesOnly = ref(false)
const busy = ref(false)
const error = ref('')
const audit = computed(() => props.status?.audit || { status: 'none', sections: [] })
const counts = computed(() => audit.value.verdict || { err_checks: 0, warn_checks: 0, clean: 0, total: 0 })
const fixNext = computed(() => (audit.value.sections || []).flatMap((section: any) =>
  section.checks.flatMap((check: any) => check.findings
    .filter((finding: any) => finding.severity === 'error')
    .map((finding: any) => ({ ...finding, check: check.title })))))
const sections = computed(() => (audit.value.sections || []).map((section: any) => ({
  ...section,
  checks: section.checks.filter((check: any) => {
    const haystack = `${check.title} ${check.summary} ${check.findings.map((f: any) => f.message).join(' ')}`.toLowerCase()
    return (!issuesOnly.value || check.glyph !== 'ok') && haystack.includes(query.value.toLowerCase())
  }),
})).filter((section: any) => section.checks.length))

async function run() {
  busy.value = true; error.value = ''
  try { await postJSON(boardUrl(props.board, 'audit')) } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
</script>

<template>
  <div class="view-scroll">
    <div class="view-heading">
      <div><span class="eyebrow">Deterministic checks</span><h2>Audit</h2><p>ERC, net contracts, rules, geometry, and DFM in one pass.</p></div>
      <button class="btn primary" :disabled="busy || audit.status === 'running'" @click="run">
        <LoaderCircle v-if="audit.status === 'running'" class="spin" :size="16" /><Play v-else :size="16" />
        {{ audit.status === 'running' ? 'Running' : 'Run audit' }}
      </button>
    </div>
    <div v-if="error" class="error-banner">{{ error }}</div>

    <div class="audit-summary">
      <div><strong :class="counts.err_checks ? 'text-error' : 'text-ok'">{{ counts.err_checks ? 'FAIL' : audit.status === 'ready' ? 'PASS' : 'NOT RUN' }}</strong><span>{{ audit.status === 'stale' ? 'Results are stale' : 'Latest board state' }}</span></div>
      <div class="summary-count error"><strong>{{ counts.err_checks }}</strong><span>Failing</span></div>
      <div class="summary-count warn"><strong>{{ counts.warn_checks }}</strong><span>Warnings</span></div>
      <div class="summary-count ok"><strong>{{ counts.clean }}</strong><span>Clean</span></div>
    </div>

    <section v-if="fixNext.length" class="fix-next">
      <div class="section-heading"><div><span class="eyebrow">Ordered by impact</span><h3>Fix next</h3></div><span class="count-badge">{{ fixNext.length }}</span></div>
      <div v-for="(item, index) in fixNext.slice(0, 12)" :key="index" class="finding-row error">
        <CircleAlert :size="16" /><div><strong>{{ item.message }}</strong><span>{{ item.check }}<template v-if="item.where"> · {{ item.where }}</template></span></div>
      </div>
    </section>

    <div class="toolbar-row">
      <label class="search-field"><Search :size="15" /><input v-model="query" placeholder="Filter checks and findings" /></label>
      <label class="toggle-control"><input v-model="issuesOnly" type="checkbox" /><span></span><Filter :size="14" /> Issues only</label>
    </div>

    <div v-if="audit.status === 'none'" class="empty-state"><CheckCircle2 :size="28" /> Run the audit to populate this workspace.</div>
    <section v-for="section in sections" :key="section.section" class="audit-section">
      <div class="section-heading compact"><h3>{{ section.section }}</h3><span>{{ section.checks.length }} checks</span></div>
      <details v-for="check in section.checks" :key="check.title" class="check-row" :open="check.glyph === 'error'">
        <summary>
          <i class="dot" :class="check.glyph"></i>
          <strong>{{ check.title }}</strong><span>{{ check.summary }}</span><ChevronDown :size="16" />
        </summary>
        <div class="check-findings">
          <div v-if="!check.findings.length" class="finding-row ok"><CheckCircle2 :size="15" /> No findings.</div>
          <div v-for="(finding, index) in check.findings" :key="index" class="finding-row" :class="finding.severity">
            <span class="severity-label">{{ finding.severity }}</span>
            <div><strong>{{ finding.message }}</strong><span v-if="finding.where || finding.detail">{{ finding.where }}<template v-if="finding.where && finding.detail"> · </template>{{ finding.detail }}</span></div>
          </div>
        </div>
      </details>
    </section>
  </div>
</template>
