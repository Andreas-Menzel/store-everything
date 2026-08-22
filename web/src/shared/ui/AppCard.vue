<script setup lang="ts">
/**
 * A surface with a heading. The frame every page's sections sit in, so spacing and the ring
 * come from tokens in one place rather than being re-decided per screen.
 */
defineProps<{ title?: string }>();
</script>

<template>
  <section
    class="rounded-(--radius-control) bg-(--color-surface) p-5 ring-1 ring-(--color-border-subtle)"
  >
    <header v-if="title || $slots.actions" class="mb-4 flex items-center justify-between gap-4">
      <h2 v-if="title" class="text-base font-semibold">{{ title }}</h2>
      <slot name="actions" />
    </header>
    <slot />
  </section>
</template>
