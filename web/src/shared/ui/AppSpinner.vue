<script setup lang="ts">
/**
 * Waiting, said out loud.
 *
 * A spinner that only spins is invisible to a screen reader, so the label is text with a live
 * region around it and the animation is decoration
 * ([F-027/FR-12](../../../../features/F-027-web-application-shell.md)).
 */
withDefaults(defineProps<{ label?: string }>(), { label: 'Loading' });
</script>

<template>
  <p role="status" class="flex items-center gap-2 text-sm text-(--color-ink-muted)">
    <span
      aria-hidden="true"
      class="size-3.5 animate-spin rounded-full border-2 border-(--color-border-subtle) border-t-(--color-accent)"
    />
    {{ label }}
  </p>
</template>
