<script setup lang="ts">
/**
 * A failure, or a fact, said in words.
 *
 * The failure case is [F-027/FR-8](../../../../features/F-027-web-application-shell.md): a
 * `problem+json` shown as its `title` and `detail`, with the request id where someone could be
 * asked to quote it. `role="alert"` on the critical tone so assistive technology hears it when it
 * appears rather than when focus happens to reach it (FR-12).
 */
import type { Failure } from '../api/problem';

const props = withDefaults(
  defineProps<{
    tone?: 'critical' | 'caution' | 'neutral';
    /** A parsed problem response. Its unattributed detail is shown; field errors belong to fields. */
    failure?: Failure;
    title?: string;
  }>(),
  // Absence is the state for both: no failure to render, and no title but the failure's own.
  { tone: 'critical', failure: undefined, title: undefined },
);

const heading = props.title ?? props.failure?.title ?? 'Something went wrong';
</script>

<template>
  <div
    :role="tone === 'critical' ? 'alert' : 'status'"
    class="rounded-(--radius-control) p-3 text-sm ring-1"
    :class="{
      'bg-(--color-surface) text-(--color-critical) ring-(--color-critical)': tone === 'critical',
      'bg-(--color-surface) text-(--color-caution) ring-(--color-caution)': tone === 'caution',
      'bg-(--color-surface) text-(--color-ink-muted) ring-(--color-border-subtle)':
        tone === 'neutral',
    }"
  >
    <p class="font-medium">{{ heading }}</p>
    <p v-if="failure?.detail" class="mt-1 text-(--color-ink)">{{ failure.detail }}</p>
    <slot />
    <p v-if="failure?.instance" class="mt-2 font-mono text-xs text-(--color-ink-muted)">
      {{ failure.instance }}
    </p>
  </div>
</template>
