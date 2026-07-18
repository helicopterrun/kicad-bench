<script setup lang="ts">
import { computed, ref } from 'vue'
import { ExternalLink, Maximize2, RefreshCw } from 'lucide-vue-next'
import { boardUrl } from '../api'

const props = defineProps<{ board: string, tick: number }>()
const key = ref(0)
const src = computed(() => `${boardUrl(props.board, 'preview/schematic.pdf')}?v=${props.tick + key.value}`)
</script>

<template>
  <div class="media-view">
    <div class="media-toolbar">
      <div><span class="eyebrow">Rendered from source</span><h2>Schematic</h2></div>
      <div class="toolbar-actions">
        <button class="icon-btn" title="Refresh preview" @click="key++"><RefreshCw :size="17" /></button>
        <a class="icon-btn" title="Open PDF in a new tab" :href="src" target="_blank"><ExternalLink :size="17" /></a>
      </div>
    </div>
    <iframe class="schematic-frame" :src="src" title="Schematic PDF preview"></iframe>
  </div>
</template>
