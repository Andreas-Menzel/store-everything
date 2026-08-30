<script setup lang="ts">
/**
 * Reading a document in the app, one page at a time
 * ([F-028/FR-6, FR-7](../../../features/F-028-thumbnails-and-previews.md)).
 *
 * Everything here comes from the **descriptor**: how many pages there are, and the URL pattern to
 * ask for one. Nothing is inferred from the media type, which is what lets this component work
 * for whatever paginated thing an extractor starts producing next.
 *
 * The interesting case is a page that does not exist yet. Only page one is rendered eagerly, so
 * asking for page seven answers `202` and queues the work — the viewer says so and retries,
 * rather than showing a broken image while a container is rendering exactly what was asked for.
 */
import { readFilePage } from '@store-everything/api-client';
import { useQuery } from '@tanstack/vue-query';
import { computed, onScopeDispose, ref, watch } from 'vue';

import { AppAlert, AppButton, AppSpinner, toFailure } from '@/shared';

const props = defineProps<{ fileId: string; version: string; pages: number }>();

const page = ref(1);
const objectUrl = ref<string | undefined>(undefined);

/** Object URLs outlive the response they came from, so each one is released when it is replaced. */
function show(blob: Blob): string {
  if (objectUrl.value) URL.revokeObjectURL(objectUrl.value);
  objectUrl.value = URL.createObjectURL(blob);
  return objectUrl.value;
}

onScopeDispose(() => {
  if (objectUrl.value) URL.revokeObjectURL(objectUrl.value);
});

/** The page is still being rendered. Not a failure — a state with a retry attached. */
class Rendering extends Error {}

const image = useQuery({
  queryKey: computed(() => ['file', props.fileId, 'page', props.version, page.value]),
  queryFn: async () => {
    const { data, error, response } = await readFilePage({
      path: { file_id: props.fileId, page: page.value },
      query: { v: props.version },
      // The endpoint answers with bytes, and the generated client would otherwise try to read
      // them as JSON.
      parseAs: 'blob',
    });
    // `202` is the queue accepting the work, and its body is empty by design.
    if (response?.status === 202) throw new Rendering();
    if (error !== undefined) throw toFailure(error, response?.status);
    return show(data as Blob);
  },
  retry: false,
  // Only while something is being rendered, and then not for long: a page takes tens of
  // milliseconds once a container picks the job up.
  refetchInterval: (query) => (query.state.error instanceof Rendering ? 700 : false),
});

const isRendering = computed(() => image.error.value instanceof Rendering);
const failure = computed(() =>
  image.error.value && !isRendering.value ? toFailure(image.error.value) : undefined,
);

watch(
  () => props.version,
  () => {
    page.value = 1;
  },
);

function go(delta: number): void {
  page.value = Math.min(Math.max(page.value + delta, 1), props.pages);
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <div
      class="flex min-h-64 items-center justify-center overflow-hidden rounded-(--radius-control) bg-(--color-surface-sunken)"
    >
      <AppSpinner v-if="image.isPending.value" label="Loading page" />
      <div v-else-if="isRendering" class="p-6 text-center text-sm text-(--color-ink-muted)">
        <AppSpinner label="Rendering this page" />
        <p class="mt-2">Rendering page {{ page }} — only the first page is made in advance.</p>
      </div>
      <AppAlert v-else-if="failure" :failure="failure" title="Could not show this page" />
      <img
        v-else-if="image.data.value"
        :src="image.data.value"
        :alt="`Page ${page} of ${pages}`"
        class="max-h-[70vh] w-full object-contain"
      />
    </div>

    <div class="flex items-center gap-3 text-sm">
      <AppButton variant="quiet" :disabled="page <= 1" aria-label="Previous page" @click="go(-1)">
        Previous
      </AppButton>
      <span aria-live="polite">Page {{ page }} of {{ pages }}</span>
      <AppButton variant="quiet" :disabled="page >= pages" aria-label="Next page" @click="go(1)">
        Next
      </AppButton>
    </div>
  </div>
</template>
