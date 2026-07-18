<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Download, ExternalLink, FileQuestion, PackageCheck, RefreshCw, Search, X } from 'lucide-vue-next'
import { boardUrl, getJSON } from '../api'

const props = defineProps<{ board: string }>()
const mode = ref<'coverage' | 'bom'>('coverage')
const parts = ref<any>({ parts: [], coverage: { total: 0, with_ds: 0, missing: [] } })
const bom = ref<any>({ rows: [], summary: {} })
const query = ref('')
const missingOnly = ref(false)
const selected = ref<any>(null)
const loading = ref(false)
const error = ref('')

const filteredParts = computed(() => parts.value.parts.filter((part: any) => {
  const match = `${part.part} ${part.value} ${part.lcsc} ${part.source}`.toLowerCase().includes(query.value.toLowerCase())
  return match && (!missingOnly.value || !part.datasheet)
}))
const filteredBom = computed(() => bom.value.rows.filter((row: any) =>
  `${row.designators?.map((d: any) => d.ref).join(' ')} ${row.value} ${row.footprint} ${row.lcsc} ${row.mpn}`.toLowerCase().includes(query.value.toLowerCase())))

async function load() {
  loading.value = true; error.value = ''
  try {
    const [p, b] = await Promise.all([
      getJSON(boardUrl(props.board, 'parts')),
      getJSON(boardUrl(props.board, 'bom')).catch((e) => ({ rows: [], summary: {}, error: e.message })),
    ])
    parts.value = p; bom.value = b
  } catch (e: any) { error.value = e.message }
  finally { loading.value = false }
}
watch(() => props.board, load)
onMounted(load)
</script>

<template>
  <div class="view-scroll table-view">
    <div class="view-heading">
      <div><span class="eyebrow">Procurement and assembly</span><h2>Parts</h2><p>Datasheet coverage and the schematic-PCB-BOM contract.</p></div>
      <div class="toolbar-actions">
        <a class="btn" :href="boardUrl(board, 'bom.csv')"><Download :size="16" /> BOM CSV</a>
        <button class="icon-btn" title="Refresh parts" @click="load"><RefreshCw :size="17" /></button>
      </div>
    </div>
    <div v-if="error" class="error-banner">{{ error }}</div>

    <div class="segmented wide"><button :class="{ active: mode === 'coverage' }" @click="mode = 'coverage'">Datasheet coverage</button><button :class="{ active: mode === 'bom' }" @click="mode = 'bom'">Assembly BOM</button></div>

    <div v-if="mode === 'coverage'" class="compact-metrics">
      <div><small>Total parts</small><strong>{{ parts.coverage.total }}</strong></div>
      <div><small>With datasheet</small><strong class="text-ok">{{ parts.coverage.with_ds }}</strong></div>
      <div><small>Missing</small><strong :class="parts.coverage.missing.length ? 'text-error' : ''">{{ parts.coverage.missing.length }}</strong></div>
    </div>
    <div v-else class="compact-metrics">
      <div><small>Components</small><strong>{{ bom.summary.parts || 0 }}</strong></div>
      <div><small>BOM lines</small><strong>{{ bom.summary.lines || 0 }}</strong></div>
      <div><small>Value mismatches</small><strong :class="bom.summary.value_mismatches ? 'text-error' : ''">{{ bom.summary.value_mismatches || 0 }}</strong></div>
      <div><small>No alternate</small><strong>{{ bom.summary.lines_missing_alternates || 0 }}</strong></div>
    </div>

    <div class="toolbar-row">
      <label class="search-field"><Search :size="15" /><input v-model="query" placeholder="Search refs, values, footprints, or part numbers" /></label>
      <label v-if="mode === 'coverage'" class="toggle-control"><input v-model="missingOnly" type="checkbox" /><span></span> Missing only</label>
    </div>

    <div class="data-table-wrap">
      <table v-if="mode === 'coverage'" class="data-table">
        <thead><tr><th>Part</th><th>Value</th><th>LCSC</th><th>Source</th><th>Datasheet</th></tr></thead>
        <tbody>
          <tr v-for="part in filteredParts" :key="`${part.part}-${part.lcsc}`" @click="selected = part">
            <td><strong>{{ part.part }}</strong></td><td>{{ part.value || '—' }}</td><td class="mono">{{ part.lcsc || '—' }}</td><td>{{ part.source || '—' }}</td>
            <td><span class="state-label" :class="part.datasheet ? 'ok' : 'error'"><PackageCheck v-if="part.datasheet" :size="14" /><FileQuestion v-else :size="14" />{{ part.datasheet ? `${part.datasheet.page_count} pages` : 'Missing' }}</span></td>
          </tr>
        </tbody>
      </table>
      <table v-else class="data-table">
        <thead><tr><th>Refs</th><th>Qty</th><th>Value</th><th>Footprint</th><th>MPN / LCSC</th><th>Contract</th></tr></thead>
        <tbody>
          <tr v-for="row in filteredBom" :key="row.group" @click="selected = row">
            <td class="mono">{{ row.designators?.map((d: any) => d.ref).join(', ') }}</td><td>{{ row.qty }}</td><td><strong>{{ row.value }}</strong></td><td>{{ row.footprint || '—' }}</td><td><span class="mono">{{ row.mpn || row.lcsc || '—' }}</span></td>
            <td><span class="state-label" :class="row.value_check === 'mismatch' || row.missing?.length ? 'error' : row.value_check === 'format-diff' ? 'warn' : 'ok'">{{ row.missing?.length ? `Missing ${row.missing.join('/')}` : row.value_check }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <aside v-if="selected" class="detail-drawer">
      <div class="drawer-head"><div><span class="eyebrow">Part detail</span><h3>{{ selected.part || selected.value }}</h3></div><button class="icon-btn" title="Close details" @click="selected = null"><X :size="18" /></button></div>
      <template v-if="selected.part">
        <dl class="detail-list"><dt>Value</dt><dd>{{ selected.value || '—' }}</dd><dt>LCSC</dt><dd>{{ selected.lcsc || '—' }}</dd><dt>Source</dt><dd>{{ selected.source || '—' }}</dd><dt>Datasheet</dt><dd>{{ selected.datasheet?.pdf || 'Not indexed' }}</dd></dl>
        <a v-if="selected.datasheet?.dsidx !== undefined" class="btn primary full" :href="boardUrl(board, `datasheets/${selected.datasheet.dsidx}/file.pdf`)" target="_blank"><ExternalLink :size="16" /> Open datasheet</a>
      </template>
      <template v-else>
        <dl class="detail-list"><dt>Footprint</dt><dd>{{ selected.footprint || '—' }}</dd><dt>Quantity</dt><dd>{{ selected.qty }}</dd><dt>Part number</dt><dd>{{ selected.mpn || selected.lcsc || '—' }}</dd><dt>Alternates</dt><dd>{{ selected.alternates?.length || 0 }}</dd></dl>
        <div class="designator-list"><div v-for="d in selected.designators" :key="d.ref"><strong>{{ d.ref }}</strong><span>{{ d.side || 'unplaced' }}<template v-if="d.rot !== null"> · {{ d.rot }}°</template></span></div></div>
      </template>
    </aside>
  </div>
</template>
