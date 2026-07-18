<script setup lang="ts">
import { computed, ref } from 'vue'
import { Layers3, RotateCcw, ZoomIn } from 'lucide-vue-next'
import { boardUrl } from '../api'

const props = defineProps<{ board: string, tick: number }>()
const side = ref<'top' | 'bottom'>('top')
const zoomed = ref(false)
const layers = [
  { id: 'B.Cu', label: 'B.Cu', color: 'E98A52' },
  { id: 'In2.Cu', label: 'In2.Cu', color: 'A9C94D' },
  { id: 'In1.Cu', label: 'In1.Cu', color: '4FC3C7' },
  { id: 'F.Cu', label: 'F.Cu', color: '3D9CB8' },
  { id: 'B.Mask', label: 'B.Mask', color: '527B9B' },
  { id: 'F.Mask', label: 'F.Mask', color: 'B8603E' },
  { id: 'B.Silkscreen', label: 'B.Silk', color: 'BBC9C9' },
  { id: 'F.Silkscreen', label: 'F.Silk', color: 'E3ECEC' },
  { id: 'Edge.Cuts', label: 'Edge', color: 'D5D7D1' },
]
const selected = ref(new Set(['F.Cu', 'F.Silkscreen', 'Edge.Cuts']))
const visible = computed(() => layers.filter((layer) => selected.value.has(layer.id)))

function layerUrl(layer: any) {
  return `${boardUrl(props.board, 'preview/pcb-layer.svg')}?layer=${encodeURIComponent(layer.id)}&color=${layer.color}&v=${props.tick}`
}
function toggle(id: string) {
  const next = new Set(selected.value)
  next.has(id) ? next.delete(id) : next.add(id)
  selected.value = next
}
function preset(next: 'top' | 'bottom') {
  side.value = next
  selected.value = new Set(next === 'top'
    ? ['F.Cu', 'F.Silkscreen', 'Edge.Cuts']
    : ['B.Cu', 'B.Silkscreen', 'Edge.Cuts'])
}
</script>

<template>
  <div class="media-view pcb-view">
    <div class="media-toolbar wrap">
      <div><span class="eyebrow">Layer browser</span><h2>PCB 2D</h2></div>
      <div class="segmented"><button :class="{ active: side === 'top' }" @click="preset('top')">Top</button><button :class="{ active: side === 'bottom' }" @click="preset('bottom')">Bottom</button></div>
      <div class="layer-controls">
        <button v-for="layer in layers" :key="layer.id" class="layer-toggle" :class="{ active: selected.has(layer.id) }" @click="toggle(layer.id)">
          <i :style="{ background: `#${layer.color}` }"></i>{{ layer.label }}
        </button>
      </div>
      <button class="icon-btn" :class="{ active: zoomed }" title="Toggle 1:1 zoom" @click="zoomed = !zoomed"><ZoomIn :size="17" /></button>
    </div>
    <div class="pcb-canvas" :class="{ mirrored: side === 'bottom', zoomed }">
      <div class="pcb-stack">
        <img v-for="layer in visible" :key="layer.id" :src="layerUrl(layer)" :alt="`${layer.label} layer`" />
      </div>
      <div v-if="!visible.length" class="empty-state"><Layers3 :size="28" /> Select at least one layer.</div>
    </div>
  </div>
</template>
