<script setup lang="ts">
import { computed, ref } from 'vue'
import { CheckCircle2, CircleAlert, Copy, LoaderCircle, Play, Rocket, ShieldCheck } from 'lucide-vue-next'
import { boardUrl, postJSON } from '../api'

const props = defineProps<{ board: string, status: any }>()
const busy = ref(false)
const copied = ref(false)
const error = ref('')
const release = computed(() => props.status?.release || { status: 'none' })
const command = computed(() => `kb release-freeze v0.1 --config kicad-bench.toml`)

async function check() {
  busy.value = true; error.value = ''
  try { await postJSON(boardUrl(props.board, 'release/check')) } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
async function copyCommand() {
  await navigator.clipboard.writeText(command.value); copied.value = true
  window.setTimeout(() => { copied.value = false }, 1500)
}
</script>

<template>
  <div class="view-scroll release-view">
    <div class="view-heading">
      <div><span class="eyebrow">Fabrication gate</span><h2>Release</h2><p>Verify ERC, DRC, layout, and BOM sourcing before creating a frozen record.</p></div>
      <button class="btn primary" :disabled="busy || release.status === 'running'" @click="check"><LoaderCircle v-if="release.status === 'running'" class="spin" :size="16" /><Play v-else :size="16" />{{ release.status === 'running' ? 'Checking' : 'Check readiness' }}</button>
    </div>
    <div v-if="error || release.status === 'error'" class="error-banner">{{ error || release.error }}</div>

    <section class="release-verdict" :class="release.status === 'ready' ? (release.ready ? 'ready' : 'blocked') : 'pending'">
      <ShieldCheck v-if="release.ready" :size="34" />
      <CircleAlert v-else-if="release.status === 'ready'" :size="34" />
      <Rocket v-else :size="34" />
      <div><span class="eyebrow">Current board</span><h3>{{ release.status === 'ready' ? (release.ready ? 'Ready to freeze' : 'Release blocked') : 'Readiness not checked' }}</h3><p>{{ release.summary || 'Run the readiness gate against the latest saved design files.' }}</p></div>
    </section>

    <section v-if="release.findings?.length" class="section-block release-checks">
      <div class="section-heading"><div><span class="eyebrow">Gate output</span><h3>Readiness checks</h3></div></div>
      <div v-for="(finding, index) in release.findings" :key="index" class="release-check" :class="finding.severity">
        <CheckCircle2 v-if="finding.severity === 'ok'" :size="17" /><CircleAlert v-else :size="17" />
        <div><strong>{{ finding.message }}</strong><span v-if="finding.detail">{{ finding.detail }}</span></div>
      </div>
    </section>

    <section class="section-block freeze-panel">
      <div class="section-heading"><div><span class="eyebrow">Explicit handoff</span><h3>Freeze from the terminal</h3></div></div>
      <p>The cockpit keeps the irreversible commit and annotated tag step in the CLI. Choose the final version, verify the entire product, then run:</p>
      <div class="command-box"><code>{{ command }}</code><button class="icon-btn" :title="copied ? 'Copied' : 'Copy command'" @click="copyCommand"><CheckCircle2 v-if="copied" :size="17" /><Copy v-else :size="17" /></button></div>
      <span class="muted-copy">The command gates every configured board, exports to <code>releases/&lt;version&gt;/</code>, commits, and tags. It never pushes.</span>
    </section>
  </div>
</template>
