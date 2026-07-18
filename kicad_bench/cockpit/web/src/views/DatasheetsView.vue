<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, ExternalLink, FileSearch, Search, ZoomIn, ZoomOut } from 'lucide-vue-next'
import { boardUrl, getJSON } from '../api'

const props = defineProps<{ board: string }>()
const items = ref<any[]>([])
const selected = ref(0)
const meta = ref<any>(null)
const page = ref(1)
const zoom = ref(100)
const query = ref('')
const hits = ref<number[]>([])
const error = ref('')
const image = computed(() => `${boardUrl(props.board, `datasheets/${selected.value}/page.png`)}?page=${page.value}&dpi=${Math.round(150 * zoom.value / 100)}`)

async function loadList() {
  try {
    const data = await getJSON<{ items: any[] }>(boardUrl(props.board, 'datasheets'))
    items.value = data.items; selected.value = 0; await loadMeta()
  } catch (e: any) { error.value = e.message }
}
async function loadMeta() {
  if (!items.value.length) { meta.value = null; return }
  meta.value = await getJSON(boardUrl(props.board, `datasheets/${selected.value}`))
  page.value = 1; hits.value = []
}
async function search() {
  if (query.value.trim().length < 2) { hits.value = []; return }
  const data = await getJSON<{ pages: number[] }>(`${boardUrl(props.board, `datasheets/${selected.value}/search`)}?q=${encodeURIComponent(query.value)}`)
  hits.value = data.pages
  if (hits.value.length) page.value = hits.value[0]
}
function move(delta: number) { page.value = Math.max(1, Math.min(meta.value?.n || 1, page.value + delta)) }
watch(selected, loadMeta)
watch(() => props.board, loadList)
onMounted(loadList)
</script>

<template>
  <div class="datasheet-view">
    <aside class="document-list">
      <div class="document-list-head"><span class="eyebrow">Local index</span><h2>Datasheets</h2></div>
      <button v-for="item in items" :key="item.i" :class="{ active: selected === item.i }" @click="selected = item.i"><FileSearch :size="16" /><span>{{ item.name }}</span></button>
      <div v-if="!items.length" class="empty-inline">No datasheets indexed.</div>
    </aside>
    <section class="document-stage">
      <div v-if="meta" class="document-toolbar">
        <strong>{{ meta.name }}</strong>
        <label class="search-field compact"><Search :size="14" /><input v-model="query" placeholder="Search this PDF" @keyup.enter="search" /></label>
        <div v-if="hits.length" class="search-hits"><button v-for="hit in hits.slice(0, 8)" :key="hit" @click="page = hit">p{{ hit }}</button></div>
        <span class="spacer"></span>
        <button class="icon-btn" title="Previous page" @click="move(-1)"><ChevronLeft :size="17" /></button><span class="page-count">{{ page }} / {{ meta.n }}</span><button class="icon-btn" title="Next page" @click="move(1)"><ChevronRight :size="17" /></button>
        <button class="icon-btn" title="Zoom out" @click="zoom = Math.max(70, zoom - 10)"><ZoomOut :size="17" /></button><span class="page-count">{{ zoom }}%</span><button class="icon-btn" title="Zoom in" @click="zoom = Math.min(150, zoom + 10)"><ZoomIn :size="17" /></button>
        <a class="icon-btn" title="Open original PDF" :href="boardUrl(board, `datasheets/${selected}/file.pdf`)" target="_blank"><ExternalLink :size="17" /></a>
      </div>
      <div v-if="error" class="error-banner">{{ error }}</div>
      <div v-if="meta" class="pdf-page"><img :key="image" :src="image" :alt="`${meta.name}, page ${page}`" :style="{ width: `${zoom}%` }" /></div>
      <div v-else class="empty-state"><FileSearch :size="30" /> Choose an indexed datasheet.</div>
    </section>
  </div>
</template>
