<script setup lang="ts">
/**
 * A labelled input that knows how to be wrong.
 *
 * One component so that three things are solved once: the label is *associated* rather than
 * merely adjacent, the error is announced through `aria-describedby` and `aria-invalid` instead
 * of only turning red, and the focus ring comes from the same token as every other control
 * ([F-027/FR-8](../../../../features/F-027-web-application-shell.md), FR-12).
 */
import { computed, useId } from 'vue';

const props = withDefaults(
  defineProps<{
    label: string;
    type?: 'text' | 'email' | 'password';
    /** The server's complaint about this field, if it had one. */
    error?: string;
    hint?: string;
    autocomplete?: string;
    required?: boolean;
    disabled?: boolean;
  }>(),
  // Absence is the state for the optional three: no complaint, no hint, no autofill hint.
  {
    type: 'text',
    required: false,
    disabled: false,
    error: undefined,
    hint: undefined,
    autocomplete: undefined,
  },
);

const value = defineModel<string>({ default: '' });

const id = useId();
const errorId = computed(() => `${id}-error`);
const hintId = computed(() => `${id}-hint`);
const describedBy = computed(() =>
  [props.error ? errorId.value : undefined, props.hint ? hintId.value : undefined]
    .filter(Boolean)
    .join(' '),
);
</script>

<template>
  <div class="flex flex-col gap-1">
    <label :for="id" class="text-sm font-medium">{{ label }}</label>
    <input
      :id="id"
      v-model="value"
      :type="type"
      :required="required"
      :disabled="disabled"
      :autocomplete="autocomplete"
      :aria-invalid="error ? 'true' : undefined"
      :aria-describedby="describedBy || undefined"
      class="rounded-(--radius-control) bg-(--color-surface) px-3 py-2 text-sm ring-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent) disabled:opacity-60"
      :class="error ? 'ring-(--color-critical)' : 'ring-(--color-border-subtle)'"
    />
    <p v-if="hint" :id="hintId" class="text-xs text-(--color-ink-muted)">{{ hint }}</p>
    <p v-if="error" :id="errorId" class="text-xs text-(--color-critical)">{{ error }}</p>
  </div>
</template>
