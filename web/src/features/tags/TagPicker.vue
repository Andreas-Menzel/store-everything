<script setup lang="ts">
/**
 * Choosing a tag by typing part of it ([F-003/FR-8](../../../features/F-003-tagging.md)).
 *
 * The completion comes from the server, ranked by how much *this* person uses each tag, and it
 * matches synonyms as well as names — so typing `automobile` can offer `car`, and the row says
 * which spelling matched. Nothing is invented locally: the vocabulary is admin-governed, so a
 * word the server does not know cannot be created from here, and the server says so.
 *
 * A combobox rather than a `<datalist>`: the options carry a usage count and a matched-by note,
 * and the keyboard behaviour (arrows to move, Enter to take, Escape to dismiss) has to be the
 * same whatever the browser is. That is also why the list is `role="listbox"` with
 * `aria-activedescendant` — the input keeps focus, and assistive technology still follows the
 * selection ([F-027/FR-12](../../../features/F-027-web-application-shell.md)).
 */
import { listTags, type TagSummary } from '@store-everything/api-client';
import { useQuery } from '@tanstack/vue-query';
import { computed, ref, useId, watch } from 'vue';

import { toFailure } from '@/shared';

const props = withDefaults(
  defineProps<{
    label?: string;
    /** Tags the subject already carries; they are dropped from the offers. */
    applied?: string[];
    disabled?: boolean;
  }>(),
  { label: 'Add a tag', applied: () => [], disabled: false },
);

const emit = defineEmits<{ picked: [tag: TagSummary]; typed: [name: string] }>();

const typed = ref('');
const debounced = ref('');
const active = ref(-1);
const listId = useId();

/**
 * A keystroke is not a request. Two hundred milliseconds is long enough that typing `invoice`
 * asks once rather than seven times, and short enough that the list feels attached to the keys.
 */
const DEBOUNCE_MS = 200;
let timer: ReturnType<typeof setTimeout> | undefined;

watch(typed, (value) => {
  if (timer !== undefined) clearTimeout(timer);
  timer = setTimeout(() => {
    debounced.value = value.trim();
    active.value = -1;
  }, DEBOUNCE_MS);
});

const completion = useQuery({
  queryKey: computed(() => ['tags', 'complete', debounced.value]),
  enabled: computed(() => debounced.value.length > 0),
  queryFn: async (): Promise<TagSummary[]> => {
    const { data, error, response } = await listTags({ query: { prefix: debounced.value } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data?.data ?? [];
  },
});

const offers = computed(() =>
  (completion.data.value ?? []).filter((tag) => !props.applied.includes(tag.id)),
);
const isOpen = computed(() => debounced.value.length > 0 && offers.value.length > 0);

function optionId(index: number): string {
  return `${listId}-option-${index}`;
}

function take(tag: TagSummary): void {
  emit('picked', tag);
  typed.value = '';
  debounced.value = '';
  active.value = -1;
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    typed.value = '';
    debounced.value = '';
    return;
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    if (!isOpen.value) return;
    event.preventDefault();
    const step = event.key === 'ArrowDown' ? 1 : -1;
    const count = offers.value.length;
    active.value = (active.value + step + count) % count;
    return;
  }
  if (event.key !== 'Enter') return;
  event.preventDefault();
  const chosen = offers.value[active.value];
  if (chosen !== undefined) {
    take(chosen);
    return;
  }
  // Nothing highlighted: send the word as typed and let the server resolve it — through the
  // alias table, or with a refusal naming the rule.
  const name = typed.value.trim();
  if (name.length > 0) {
    emit('typed', name);
    typed.value = '';
    debounced.value = '';
  }
}
</script>

<template>
  <div class="relative flex flex-col gap-1">
    <label :for="listId" class="text-sm font-medium">{{ label }}</label>
    <input
      :id="listId"
      v-model="typed"
      type="text"
      role="combobox"
      autocomplete="off"
      :disabled="disabled"
      :aria-expanded="isOpen"
      aria-controls="tag-options"
      :aria-activedescendant="active >= 0 ? optionId(active) : undefined"
      class="rounded-md border border-(--color-border-subtle) bg-(--color-surface) px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent) disabled:opacity-60"
      placeholder="Start typing…"
      @keydown="onKeydown"
    />
    <ul
      v-if="isOpen"
      id="tag-options"
      role="listbox"
      aria-label="Matching tags"
      class="absolute top-full z-10 mt-1 w-full overflow-hidden rounded-md border border-(--color-border-subtle) bg-(--color-surface) shadow-lg"
    >
      <li
        v-for="(offer, index) in offers"
        :id="optionId(index)"
        :key="offer.id"
        role="option"
        :aria-selected="index === active"
        :class="[
          'flex cursor-pointer items-baseline justify-between gap-3 px-3 py-2 text-sm',
          index === active ? 'bg-(--color-surface-sunken)' : '',
        ]"
        @mousedown.prevent="take(offer)"
        @mouseenter="active = index"
      >
        <span>
          {{ offer.name }}
          <span
            v-if="offer.matched_alias && offer.matched"
            class="text-xs text-(--color-ink-muted)"
          >
            — matched “{{ offer.matched }}”
          </span>
        </span>
        <span class="text-xs text-(--color-ink-muted)">
          {{ offer.usage.files + offer.usage.folders }} used
        </span>
      </li>
    </ul>
  </div>
</template>
