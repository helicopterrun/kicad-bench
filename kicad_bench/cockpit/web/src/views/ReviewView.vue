<script setup lang="ts">
import { computed, ref } from 'vue'
import { BookOpenCheck, ExternalLink, LoaderCircle, Play, ShieldQuestion } from 'lucide-vue-next'
import { boardUrl, postJSON } from '../api'

const props = defineProps<{ board: string, status: any }>()
const busy = ref(false)
const error = ref('')
const review = computed(() => props.status?.review || { status: 'none' })
const findings = computed(() => {
  if (review.value.ics) {
    return review.value.ics.flatMap((ic: any) => (ic.findings || []).map((finding: any) => ({
      ...finding, ref: ic.ref, mpn: ic.mpn,
    })))
  }
  return review.value.findings || review.value.results || []
})
function citations(finding: any) {
  return (finding.pages || []).map((page: any) =>
    typeof page === 'object' ? `${page.slug} p${page.page}` : `p${page}`).join(', ')
}

async function run() {
  busy.value = true; error.value = ''
  try { await postJSON(boardUrl(props.board, 'review')) } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
</script>

<template>
  <div class="view-scroll">
    <div class="view-heading">
      <div><span class="eyebrow">Datasheet-grounded reasoning</span><h2>Review</h2><p>Semantic findings with evidence that deterministic checks cannot express.</p></div>
      <button class="btn primary" :disabled="busy || review.status === 'running'" @click="run">
        <LoaderCircle v-if="review.status === 'running'" class="spin" :size="16" /><Play v-else :size="16" />
        {{ review.status === 'running' ? 'Reviewing' : 'Run review' }}
      </button>
    </div>
    <div v-if="error || review.status === 'error'" class="error-banner">{{ error || review.error }}</div>
    <div v-if="review.status === 'none'" class="empty-state">
      <ShieldQuestion :size="30" /><strong>No review artifact yet</strong>
      <span>The review uses the configured Claude backend and writes cited findings to <code>.cockpit/review.json</code>.</span>
    </div>
    <div v-else-if="review.status === 'running'" class="empty-state"><LoaderCircle class="spin" :size="30" /> Reviewing schematic and datasheet evidence.</div>
    <template v-else>
      <div class="review-banner" :class="review.stale ? 'warn' : 'ok'">
        <BookOpenCheck :size="20" /><div><strong>{{ review.stale ? 'Review is stale' : 'Review is current' }}</strong><span>{{ findings.length }} finding{{ findings.length === 1 ? '' : 's' }}</span></div>
      </div>
      <article v-for="(finding, index) in findings" :key="index" class="review-finding">
        <div class="review-meta"><span class="severity-label" :class="finding.severity">{{ finding.severity || 'review' }}</span><span v-if="finding.ref">{{ finding.ref }}<template v-if="finding.mpn"> · {{ finding.mpn }}</template></span><span v-if="finding.confidence">{{ finding.confidence }} confidence</span></div>
        <h3>{{ finding.title || finding.message || `Finding ${index + 1}` }}</h3>
        <p>{{ finding.detail || finding.why || finding.reason || finding.description }}</p>
        <div v-if="finding.recommendation" class="recommendation"><strong>Recommendation</strong><span>{{ finding.recommendation }}</span></div>
        <div v-if="citations(finding) || finding.citation || finding.source" class="citation"><ExternalLink :size="14" />{{ citations(finding) || finding.citation || finding.source }}</div>
      </article>
      <div v-if="!findings.length" class="empty-state"><BookOpenCheck :size="28" /> No review findings.</div>
    </template>
  </div>
</template>
