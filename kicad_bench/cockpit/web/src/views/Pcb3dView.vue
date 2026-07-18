<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Box, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import { boardUrl, getJSON } from '../api'

const props = defineProps<{ board: string, tick: number }>()
const side = ref<'top' | 'bottom'>('top')
const render = ref<any>({ status: 'none' })
const imageKey = ref(0)
let timer: number | undefined

async function poll() {
  try {
    render.value = await getJSON(`${boardUrl(props.board, 'preview/pcb3d')}?side=${side.value}`)
    if (render.value.status === 'ready') imageKey.value++
  } catch (e: any) { render.value = { status: 'error', error: e.message } }
}
watch([side, () => props.board], poll)
onMounted(() => { poll(); timer = window.setInterval(poll, 2500) })
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div class="media-view">
    <div class="media-toolbar">
      <div><span class="eyebrow">KiCad raytrace</span><h2>PCB 3D</h2></div>
      <div class="segmented"><button :class="{ active: side === 'top' }" @click="side = 'top'">Top</button><button :class="{ active: side === 'bottom' }" @click="side = 'bottom'">Bottom</button></div>
      <button class="icon-btn" title="Refresh render status" @click="poll"><RefreshCw :size="17" /></button>
    </div>
    <div class="render-stage">
      <img v-if="render.status === 'ready'" :src="`${boardUrl(board, 'preview/pcb3d.png')}?side=${side}&v=${imageKey}`" :alt="`${side} 3D board render`" />
      <div v-else-if="render.status === 'rendering'" class="empty-state"><LoaderCircle class="spin" :size="30" /><strong>Rendering {{ side }} view</strong><span>KiCad raytracing continues in the background.</span></div>
      <div v-else-if="render.status === 'error'" class="empty-state text-error"><Box :size="30" />{{ render.error }}</div>
      <div v-else class="empty-state"><Box :size="30" /> No PCB is available for 3D rendering.</div>
    </div>
  </div>
</template>
