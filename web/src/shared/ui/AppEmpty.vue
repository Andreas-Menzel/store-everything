<script setup lang="ts">
/**
 * Nothing here, and why.
 *
 * An empty list and a failed request look identical if both render as blank, so emptiness gets a
 * component of its own — with room for the action that would end it.
 */
defineProps<{ title: string; detail?: string }>();
</script>

<template>
  <div
    class="rounded-(--radius-control) bg-(--color-surface) p-6 text-center ring-1 ring-(--color-border-subtle)"
  >
    <p class="text-sm font-medium">{{ title }}</p>
    <p v-if="detail" class="mt-1 text-sm text-(--color-ink-muted)">{{ detail }}</p>
    <div v-if="$slots.default" class="mt-4 flex justify-center"><slot /></div>
  </div>
</template>
