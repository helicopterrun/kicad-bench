<script setup lang="ts">
import {
  Activity, Box, CheckCircle2, ClipboardCheck, FileSearch, FileText,
  Gauge, Layers3, ListChecks, PackageSearch, Rocket, Workflow, X,
} from 'lucide-vue-next'
import type { Board } from '../api'

defineProps<{
  open: boolean
  product: any
  boards: Board[]
  activeBoard: string
  activeView: string
}>()
const emit = defineEmits(['close', 'board', 'view'])

const nav = [
  { id: 'overview', label: 'Overview', icon: Gauge },
  { id: 'audit', label: 'Audit', icon: ListChecks },
  { id: 'review', label: 'Review', icon: ClipboardCheck },
  { id: 'schematic', label: 'Schematic', icon: Workflow },
  { id: 'pcb2d', label: 'PCB 2D', icon: Layers3 },
  { id: 'pcb3d', label: 'PCB 3D', icon: Box },
  { id: 'parts', label: 'Parts', icon: PackageSearch },
  { id: 'datasheets', label: 'Datasheets', icon: FileSearch },
  { id: 'stage', label: 'Stage', icon: Activity },
  { id: 'release', label: 'Release', icon: Rocket },
]
</script>

<template>
  <div v-if="open" class="sidebar-scrim" @click="emit('close')"></div>
  <aside class="sidebar" :class="{ open }">
    <div class="brand-row">
      <div class="brand-mark"><FileText :size="18" /></div>
      <div class="brand-copy">
        <strong>KiCad Cockpit</strong>
        <span>{{ product?.name || 'Workbench' }}</span>
      </div>
      <button class="icon-btn mobile-only" title="Close navigation" @click="emit('close')">
        <X :size="18" />
      </button>
    </div>

    <div class="sidebar-label">Board</div>
    <select class="board-select" :value="activeBoard" @change="emit('board', ($event.target as HTMLSelectElement).value)">
      <option v-for="board in boards" :key="board.id" :value="board.id">{{ board.name }}</option>
    </select>

    <nav class="primary-nav" aria-label="Board workspace">
      <button
        v-for="item in nav"
        :key="item.id"
        :class="{ active: activeView === item.id }"
        @click="emit('view', item.id); emit('close')"
      >
        <component :is="item.icon" :size="17" />
        <span>{{ item.label }}</span>
      </button>
    </nav>

    <div class="sidebar-foot">
      <CheckCircle2 :size="15" />
      <span>Read-first workflow</span>
    </div>
  </aside>
</template>
