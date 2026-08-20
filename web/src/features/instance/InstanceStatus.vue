<script setup lang="ts">
import { computed } from 'vue';

import type { ReadinessState } from './readiness';

/**
 * Presentational: it renders a readiness state and knows nothing about how that state
 * was obtained, so every state is testable without a network.
 */
const props = defineProps<{ state: ReadinessState }>();

const LABELS: Record<ReadinessState, string> = {
  pending: 'Checking the instance…',
  ready: 'Instance ready',
  unavailable: 'Instance unavailable',
};

const TONES: Record<ReadinessState, string> = {
  pending: 'bg-(--color-ink-muted)',
  ready: 'bg-(--color-positive)',
  unavailable: 'bg-(--color-critical)',
};

const label = computed(() => LABELS[props.state]);
const tone = computed(() => TONES[props.state]);
</script>

<template>
  <p class="flex items-center gap-2 text-sm text-(--color-ink)" :data-state="state">
    <span class="size-2.5 rounded-full" :class="tone" aria-hidden="true" />
    <span>{{ label }}</span>
  </p>
</template>
