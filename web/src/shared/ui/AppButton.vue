<script setup lang="ts">
/**
 * The one button. Variants are data, not internal branches
 * (11-engineering-standards.md § when to abstract).
 */
withDefaults(
  defineProps<{
    variant?: 'primary' | 'quiet';
    type?: 'button' | 'submit';
    disabled?: boolean;
  }>(),
  { variant: 'primary', type: 'button', disabled: false },
);
</script>

<template>
  <button
    :type="type"
    :disabled="disabled"
    class="inline-flex items-center justify-center rounded-(--radius-control) px-3.5 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent) disabled:cursor-not-allowed disabled:opacity-60"
    :class="
      variant === 'primary'
        ? 'bg-(--color-accent) text-(--color-accent-contrast) hover:opacity-90'
        : 'bg-(--color-surface) text-(--color-ink) ring-1 ring-(--color-border-subtle) hover:bg-(--color-surface-sunken)'
    "
  >
    <slot />
  </button>
</template>
